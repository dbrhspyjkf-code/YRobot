import Foundation

/// Programmable transport used in unit tests. Holds a queue of
/// pre-canned responses and dispatches them when the session sends
/// a command. Lets tests exercise the full session and provisioner
/// state machines without ever touching CoreBluetooth.
@MainActor
public final class MockBLETransport: ReachyBLETransport, @unchecked Sendable {

    public struct PlannedResponse: Sendable {
        /// What the robot replies on the next notification. If
        /// `immediate` is non-nil, that string is also written back
        /// synchronously after the write (mimicking commands that
        /// reply before returning).
        public let immediate: String?
        public let notification: String?
        public init(immediate: String? = nil, notification: String? = nil) {
            self.immediate = immediate
            self.notification = notification
        }
    }

    public private(set) var commands: [String] = []
    public var responses: [String: PlannedResponse] = [:]
    public var onConnect: (@Sendable () async throws -> Void)?
    public var onDiscover: (@Sendable () async throws -> Void)?
    public var onSubscribe: (@Sendable () async throws -> Void)?
    public var subscribeCallCount = 0
    public var writeCallCount = 0
    public var connected: Bool = false
    public var discovered: Bool = false
    public var subscribed: Bool = false
    public var disconnected = false

    private var onPeripheralCallback: ((UUID, String) -> Void)?
    private var onNotificationCallback: ((Data) -> Void)?

    public init() {}

    public func feed(_ notification: String) {
        guard let data = notification.data(using: .utf8) else { return }
        onNotificationCallback?(data)
    }

    public func startScan(onPeripheral: @escaping @Sendable (UUID, String) -> Void) async {
        onPeripheralCallback = onPeripheral
    }

    public func stopScan() async {
        onPeripheralCallback = nil
    }

    public func connect(_ peripheralID: UUID) async throws {
        if let onConnect {
            try await onConnect()
        }
        connected = true
    }

    public func discoverServices() async throws {
        if let onDiscover {
            try await onDiscover()
        }
        discovered = true
    }

    public func subscribe(onNotify: @escaping @Sendable (Data) -> Void) async throws {
        if let onSubscribe {
            try await onSubscribe()
        }
        onNotificationCallback = onNotify
        subscribeCallCount += 1
        subscribed = true
    }

    public func writeCommand(_ text: String) async throws {
        writeCallCount += 1
        commands.append(text)
        // Match the exact key first, then the wildcard, then any
        // key with a trailing " *" so tests can register
        // catch-all responses for "WIFI_CONNECT_ENC *" without
        // caring about the exact JSON body.
        let plan = responses[text]
            ?? responses["*"]
            ?? responses.first(where: { key, _ in
                key.hasSuffix(" *") && text.hasPrefix(String(key.dropLast(2)))
            })?.value
        if let plan {
            if let immediate = plan.immediate {
                if let data = immediate.data(using: .utf8) {
                    onNotificationCallback?(data)
                }
            }
            if let notification = plan.notification {
                if let data = notification.data(using: .utf8) {
                    onNotificationCallback?(data)
                }
            }
        }
    }

    public func disconnect() async {
        disconnected = true
        connected = false
    }
}
