import Foundation

/// Higher-level BLE session built on `ReachyBLETransport`. Owns the
/// PIN window, the async-ACK command queue, and the small vocabulary
/// the Wi-Fi provisioner needs:
///
///   authenticate(pin:)         — `PIN_…` and a synchronous reply
///   wifiStatus()               — public command, may return a known
///                                list of SSIDs only when authed
///   wifiKeyex()                — public, returns (kid, pk)
///   wifiScan()                 — auth required, async
///   wifiConnect(ssid:psk:pin:) — auth required, async
///
/// The transport handles the underlying GATT; the session handles the
/// protocol layering, the auth window, and the difference between
/// sync and async replies.
@MainActor
public final class ReachyBLESession: @unchecked Sendable {

    public enum SessionError: Error, Sendable, Equatable {
        case notConnected
        case notSubscribed
        case noAuth
        case unknownError(String)
        case protocolError(String)
    }

    public struct WifiKeyex: Codable, Sendable, Equatable {
        public let kid: String
        public let pk: String
        public let alg: String?
    }

    public struct WifiStatus: Codable, Sendable, Equatable {
        public let mode: String
        public let connected: String?
        public let error: String?
        public let known: [String]?
    }

    private let transport: ReachyBLETransport
    private var continuation: AsyncStream<Data>.Continuation?
    private var notifications: AsyncStream<Data>
    private var notificationTask: Task<Void, Never>?
    /// True once `subscribe()` has successfully attached the
    /// notification handler. The transport's underlying GATT
    /// connection is owned outside the session, so this flag is the
    /// only reliable way to know whether subsequent commands will
    /// be received. Cleared by `disconnect()`.
    private var subscribed: Bool = false
    /// 5 minutes — matches the daemon's session TTL.
    private let authTtl: TimeInterval = 300
    private var authedUntil: Date?

    public init(transport: ReachyBLETransport) {
        self.transport = transport
        var localContinuation: AsyncStream<Data>.Continuation!
        self.notifications = AsyncStream<Data> { cont in
            localContinuation = cont
        }
        self.continuation = localContinuation
    }

    public var isConnected: Bool { subscribed }
    public var isAuthed: Bool {
        guard let until = authedUntil else { return false }
        return until > Date()
    }

    /// Subscribe to response notifications. After this point, every
    /// notification received from the robot is queued into an
    /// `AsyncStream<Data>`; commands pop the head of that queue
    /// (waiting if empty) when they need an async reply.
    public func subscribe() async throws {
        try await transport.subscribe { [weak self] data in
            guard let self else { return }
            Task { @MainActor in
                self.continuation?.yield(data)
            }
        }
        subscribed = true
    }

    /// Mark the session as no longer connected. The transport's
    /// underlying GATT connection is owned by the caller; this just
    /// clears local state so subsequent commands fail with
    /// `notConnected` instead of waiting on a dead link.
    public func disconnect() {
        subscribed = false
        authedUntil = nil
    }

    /// Wait for the next notification, with a timeout. Returns the
    /// payload as a UTF-8 string. Used by the async-ack commands.
    private func nextNotification(timeout: TimeInterval) async throws -> String {
        return try await withThrowingTaskGroup(of: String?.self) { group in
            group.addTask { [notifications] in
                for await data in notifications {
                    return String(data: data, encoding: .utf8)
                }
                return nil
            }
            group.addTask {
                try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                return nil
            }
            let first = try await group.next() ?? nil
            group.cancelAll()
            guard let value = first, !value.isEmpty else {
                throw SessionError.protocolError("notification timeout")
            }
            return value
        }
    }

    /// Send a command and return the first reply. Most BLE commands
    /// (PIN, KEYEX, PING) reply synchronously; for those this returns
    /// the immediate reply. Async commands use `nextNotification`.
    private func sendAndAwait(_ command: String) async throws -> String {
        try await transport.writeCommand(command)
        return try await nextNotification(timeout: 5)
    }

    // MARK: - Auth

    public func authenticate(pin: String) async throws {
        guard pin.count == 5 else {
            throw SessionError.protocolError("PIN must be 5 characters")
        }
        let reply = try await sendAndAwait("PIN_\(pin)")
        if reply.contains("Authenticated") {
            authedUntil = Date().addingTimeInterval(authTtl)
        } else if reply.contains("Bad credentials") {
            throw SessionError.protocolError("wrong PIN")
        } else {
            throw SessionError.protocolError(reply)
        }
    }

    // MARK: - Public commands

    public func wifiStatus() async throws -> WifiStatus {
        let reply = try await sendAndAwait("WIFI_STATUS")
        return try Self.parseStatus(reply)
    }

    public func wifiKeyex() async throws -> WifiKeyex {
        let reply = try await sendAndAwait("WIFI_KEYEX")
        guard let data = reply.data(using: .utf8),
              let obj = try? JSONDecoder().decode(WifiKeyex.self, from: data) else {
            throw SessionError.protocolError("WIFI_KEYEX parse failed: \(reply)")
        }
        return obj
    }

    // MARK: - Authed commands (async)

    /// `WIFI_SCAN` always returns asynchronously. The transport
    /// reports an immediate `OK: working` ack after the write; the
    /// real result arrives on the next notification. We retry up to
    /// `timeoutSeconds` for the real payload.
    public func wifiScan(timeoutSeconds: TimeInterval = 15) async throws -> [String] {
        guard isAuthed else { throw SessionError.noAuth }
        _ = try await transport.writeCommand("WIFI_SCAN")
        let reply = try await nextNotification(timeout: timeoutSeconds)
        if reply.hasPrefix("ERROR:") {
            throw SessionError.protocolError(reply)
        }
        guard let data = reply.data(using: .utf8),
              let arr = try? JSONDecoder().decode([String].self, from: data) else {
            throw SessionError.protocolError("WIFI_SCAN parse failed: \(reply)")
        }
        return arr
    }

    public func wifiConnect(sealed: WifiSealer.SealedPayload, ssid: String, timeoutSeconds: TimeInterval = 90) async throws {
        guard isAuthed else { throw SessionError.noAuth }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.withoutEscapingSlashes]
        let json = try encoder.encode(sealed)
        let cmd = "WIFI_CONNECT_ENC " + (String(data: json, encoding: .utf8) ?? "")
        _ = try await transport.writeCommand(cmd)
        // The first reply is the immediate ack. If the robot reports
        // an error synchronously (wrong PIN, busy, etc.), we surface
        // it immediately rather than waiting for the polling loop to
        // time out. The real connection result lands on the next
        // status change.
        let firstReply = (try? await nextNotification(timeout: 5)) ?? ""
        if firstReply.hasPrefix("ERROR:") {
            throw SessionError.protocolError(firstReply)
        }
        let deadline = Date().addingTimeInterval(timeoutSeconds)
        while Date() < deadline {
            let status = try await wifiStatus()
            if status.mode == "wlan" && status.connected == ssid {
                return
            }
            if let err = status.error, !err.isEmpty {
                throw SessionError.protocolError("wifi error: \(err)")
            }
            try await Task.sleep(nanoseconds: 1_500_000_000)
        }
        throw SessionError.protocolError("wifi connect timeout")
    }

    private static func parseStatus(_ reply: String) throws -> WifiStatus {
        guard let data = reply.data(using: .utf8),
              let obj = try? JSONDecoder().decode(WifiStatus.self, from: data) else {
            throw SessionError.protocolError("WIFI_STATUS parse failed: \(reply)")
        }
        return obj
    }
}
