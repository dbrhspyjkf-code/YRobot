import Foundation
import Observation

/// Tracks the current robot's reachability and the last-known state.
///
/// One instance per connected robot. Feature view-models read `availability`,
/// `status`, etc. and call `refresh()` to poll. Discovery / Network view writes
/// `connect(to:)`; Overview / Settings read state.
///
/// Per spec §5.4 the daemon and YRobot are probed independently — a partial
/// failure does not make the whole app appear offline.
@Observable
@MainActor
public final class RobotSession {

    public enum SubsystemState: Sendable, Equatable {
        case unknown
        case probing
        case available
        case unavailable(reason: String, since: Date)
    }

    public struct Availability: Sendable, Equatable {
        public var daemon: SubsystemState = .unknown
        public var yrobot: SubsystemState = .unknown

        public var bothAvailable: Bool {
            daemon == .available && yrobot == .available
        }
    }

    /// Four-state connection outcome. Replaces the old boolean
    /// "is at least one port up" check; the iOS UI now distinguishes
    /// between the two partial-failure cases so the operator can tell
    /// which side is down and where to look.
    public enum ConnectionResult: Sendable, Equatable {
        case bothAvailable
        case daemonOnly(reason: String)
        case yrobotOnly(reason: String)
        case unavailable(reason: String)

        public var isConnected: Bool {
            switch self {
            case .bothAvailable, .daemonOnly, .yrobotOnly: true
            case .unavailable: false
            }
        }

        /// Short user-facing message describing the broken subsystem, or
        /// nil when both ports answered.
        public var subsystemFailureMessage: String? {
            switch self {
            case .bothAvailable: nil
            case .daemonOnly(let r): "YRobot dashboard unreachable: \(r)"
            case .yrobotOnly(let r): "Reachy daemon unreachable: \(r)"
            case .unavailable(let r): r
            }
        }
    }

    /// Result of a bounded rediscovery loop. The session attempts
    /// `refresh()` repeatedly with exponential backoff and stops either
    /// when both subsystems answer or when `maxAttempts` is reached.
    public enum RediscoveryOutcome: Sendable, Equatable {
        case connected
        case exhausted
    }

    public private(set) var endpoint: RobotEndpoint?
    public private(set) var availability: Availability = Availability()
    public private(set) var status: YRobotStatus?
    public private(set) var daemonStatus: DaemonStatus?
    public private(set) var lastSuccessfulFetch: Date?
    public private(set) var lastYRobotFetch: Date?
    public private(set) var lastDaemonFetch: Date?
    public private(set) var hardwareId: String?
    public private(set) var robotName: String?
    public private(set) var conversationTurns: [ConversationTurn] = []
    public private(set) var conversationBackend: ConversationBackendState?
    public private(set) var conversationVoice: ConversationVoiceState?

    public var isConnected: Bool { availability.bothAvailable }

    private let preferences: RobotPreferences
    private let session: URLSession
    private var daemonClient: APIClient?
    private var yrobotClient: APIClient?

    public init(
        preferences: RobotPreferences = RobotPreferences(),
        session: URLSession = .shared
    ) {
        self.preferences = preferences
        self.session = session
    }

    // MARK: - Connect

    /// Connect to the given endpoint. Probes both ports and updates
    /// `availability` accordingly. Returns `true` if at least one port
    /// answered; the caller may still choose to surface the partial failure.
    @discardableResult
    public func connect(
        to endpoint: RobotEndpoint,
        hardwareId: String? = nil,
        robotName: String? = nil
    ) async -> ConnectionResult {
        self.endpoint = endpoint
        self.hardwareId = hardwareId
        self.robotName = robotName
        self.availability = Availability()
        self.status = nil
        self.daemonStatus = nil
        self.lastSuccessfulFetch = nil

        do {
            self.daemonClient = APIClient(baseURL: try endpoint.daemonBaseURL(), session: session)
        } catch {
            availability.daemon = .unavailable(reason: Self.describe(error), since: .now)
        }
        do {
            self.yrobotClient = APIClient(baseURL: try endpoint.yrobotBaseURL(), session: session)
        } catch {
            availability.yrobot = .unavailable(reason: Self.describe(error), since: .now)
        }

        async let daemonResult: Void = probeDaemon()
        async let yrobotResult: Void = probeYRobot()
        _ = await (daemonResult, yrobotResult)

        let result = connectionResult()

        // Persist preferred robot only when at least one port worked and we
        // have a stable identifier (Bonjour name + hardware id). We do not
        // save on a full `unavailable` — otherwise the next launch would
        // auto-retry a host that is genuinely offline.
        if result.isConnected, let hardwareId, case .bonjourName(let name) = endpoint.host {
            // `name` is already the .local mDNS hostname (e.g. "reachy-mini.local").
            // Don't append another ".local" — that produces "reachy-mini.local.local"
            // which does not resolve via Avahi.
            preferences.save(.init(
                serviceName: name,
                hardwareId: hardwareId,
                lastHost: name
            ))
        }

        return result
    }

    /// Translates the per-subsystem availability into a single
    /// `ConnectionResult` for the UI.
    public func connectionResult() -> ConnectionResult {
        switch (availability.daemon, availability.yrobot) {
        case (.available, .available):
            return .bothAvailable
        case (.available, .unavailable(let reason, _)):
            return .daemonOnly(reason: reason)
        case (.unavailable(let reason, _), .available):
            return .yrobotOnly(reason: reason)
        case (.unavailable(let dReason, _), .unavailable(let yReason, _)):
            return .unavailable(reason: "daemon: \(dReason); yrobot: \(yReason)")
        default:
            // Either side is still `.probing` or `.unknown`. Treat as
            // not-yet-fully-known; the UI can show "connecting".
            return .unavailable(reason: "not yet probed")
        }
    }

    /// Bounded rediscovery loop. Calls `refresh()` until both subsystems
    /// answer or `maxAttempts` is exhausted. `baseDelay` is in seconds;
    /// the loop doubles the delay on each failure (capped at 8s) so a
    /// fresh boot or a momentary Wi-Fi blip is recovered quickly without
    /// hammering the network.
    @discardableResult
    public func rediscover(
        maxAttempts: Int = 5,
        baseDelay: TimeInterval = 2.0
    ) async -> RediscoveryOutcome {
        guard endpoint != nil else { return .exhausted }
        var delay = baseDelay
        for _ in 0..<maxAttempts {
            await refresh()
            if availability.bothAvailable {
                return .connected
            }
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            delay = min(delay * 2, 8.0)
        }
        return .exhausted
    }

    public func disconnect() {
        endpoint = nil
        availability = Availability()
        status = nil
        daemonStatus = nil
        lastSuccessfulFetch = nil
        lastYRobotFetch = nil
        lastDaemonFetch = nil
        hardwareId = nil
        robotName = nil
        conversationTurns = []
        conversationBackend = nil
        conversationVoice = nil
        daemonClient = nil
        yrobotClient = nil
    }

    public func probeDaemon() async {
        guard let daemonClient else {
            availability.daemon = .unavailable(reason: "no client", since: .now)
            return
        }
        availability.daemon = .probing
        do {
            let s: DaemonStatus = try await daemonClient.get("/api/daemon/status")
            daemonStatus = s
            availability.daemon = .available
            lastSuccessfulFetch = .now
            lastDaemonFetch = .now
        } catch {
            availability.daemon = .unavailable(reason: error.localizedDescription, since: .now)
        }
    }

    public func probeYRobot() async {
        guard let yrobotClient else {
            availability.yrobot = .unavailable(reason: "no client", since: .now)
            return
        }
        availability.yrobot = .probing
        do {
            let s: YRobotStatus = try await yrobotClient.get("/api/status")
            status = s
            availability.yrobot = .available
            lastSuccessfulFetch = .now
            lastYRobotFetch = .now
        } catch {
            availability.yrobot = .unavailable(reason: error.localizedDescription, since: .now)
        }
    }

    /// Fetches the recent chat log (spec §8 Overview "Recent conversation
    /// turns"). The server already filters to chat markers via `?filter=chat`;
    /// we still re-verify the marker client-side. Called on a 2-second cadence
    /// from the Overview view; failures are silent so a transient blip keeps
    /// the previous turns visible.
    public func probeChatLogs(lines: Int = 100) async {
        guard let yrobotClient else { return }
        let query = [
            URLQueryItem(name: "filter", value: "chat"),
            URLQueryItem(name: "lines", value: String(lines)),
        ]
        do {
            let resp: LogsResponse = try await yrobotClient.get("/api/logs", query: query)
            conversationTurns = ConversationTurn.parse(resp.logs)
        } catch {
            // Keep previous turns; spec §9 "Connection loss shows the last
            // successful timestamp".
        }
    }

    /// Polls status subsystems. Used by Overview on its 5-second cadence.
    /// No-op when no endpoint is connected.
    public func refresh() async {
        guard endpoint != nil else { return }
        async let d: Void = probeDaemon()
        async let y: Void = probeYRobot()
        _ = await (d, y)
    }

    // MARK: - Media controls (Stage 4)
    //
    // These methods wrap single calls on the YRobot port. Per spec §8 the UI
    // must show the value the robot returns (not what the user typed), so
    // each call returns the authoritative response.

    public func setCameraEnabled(_ enabled: Bool) async throws -> Bool {
        let env: CameraStateEnvelope = try await requireYRobot()
            .put("/api/camera/state", body: CameraStateSetRequest(running: enabled))
        return env.state.running
    }

    public func currentFrame() async throws -> Data {
        try await requireYRobot().getData("/api/camera/frame")
    }

    @discardableResult
    public func setVolume(percent: Int) async throws -> Int {
        let env: VolumeEnvelope = try await requireYRobot()
            .put("/api/volume", body: VolumeSetRequest(percent: percent))
        return env.volume.percent
    }

    public func fetchVolumeConfig() async throws -> (percent: Int, min: Int, max: Int, alsa: [Int]) {
        let env: VolumeEnvelope = try await requireYRobot().get("/api/volume")
        return (env.volume.percent, env.volume.min_percent, env.volume.max_percent, env.volume.range)
    }

    // MARK: - Motions (Stage 5)

    public func fetchMotions() async throws -> MotionList {
        try await requireYRobot().get("/api/motion")
    }

    /// Plays a motion by name. Throws `APIError.validation` with the server
    /// detail when the move is unknown (the dashboard does the same). Does
    /// not retry automatically on app-lock / daemon-busy (spec §8 Motions).
    public func playMotion(name: String) async throws -> MotionPlayResponse {
        try await requireYRobot()
            .post("/api/motion", body: MotionPlayRequest(move: name))
    }

    // MARK: - Logs (Stage 5)

    public func fetchLogs(
        minLevel: LogMinLevel = .info,
        kind: LogKindFilter? = nil,
        lines: Int = 200
    ) async throws -> LogsResponse {
        var query: [URLQueryItem] = [
            URLQueryItem(name: "lines", value: String(lines)),
            URLQueryItem(name: "min_level", value: minLevel.rawValue),
        ]
        if let kind { query.append(URLQueryItem(name: "filter", value: kind.rawValue)) }
        return try await requireYRobot().get("/api/logs", query: query)
    }

    // MARK: - Daemon & Power (Stage 5)

    @discardableResult
    public func sendDaemonAction(_ action: ReachyDaemonAction) async throws -> ReachyDaemonActionResponse {
        try await requireYRobot()
            .post("/api/reachy-daemon/action",
                  body: ReachyDaemonActionRequest(action: action))
    }

    /// Restart the YRobot service (the dashboard itself). The browser sees
    /// a graceful response before systemd SIGTERMs the process — callers
    /// must show a confirmation and not retry on timeout (spec §8).
    public func restartYRobot() async throws {
        try await requireYRobot()
            .postDiscarding("/api/system/restart",
                            body: Optional<ReachyDaemonActionRequest>.none)
    }

    /// Reboot or power off the robot. The server sends the daemon to sleep
    /// first (`_request_reachy_sleep_before_power`), so the response may
    /// arrive after a delay.
    public func systemPower(_ action: SystemPowerAction) async throws {
        try await requireYRobot()
            .postDiscarding("/api/system/power",
                            body: SystemPowerRequest(action: action))
    }

    @discardableResult
    public func setMicUpload(enabled: Bool) async throws -> Bool {
        let env: AudioInputEnvelope = try await requireYRobot()
            .put("/api/audio/input", body: AudioInputSetRequest(enabled: enabled))
        return env.audio_input.enabled
    }

    /// Returns the VAD `rms_min` the robot accepted. Throws
    /// `APIError.unavailableSubsystem` if persistence fails (the server uses
    /// HTTP 503 when it can't write to the env file).
    @discardableResult
    public func setVAD(rms: Double) async throws -> Double {
        let env: VADEnvelope = try await requireYRobot()
            .put("/api/audio/vad", body: VADSetRequest(rms_min: rms))
        return env.vad.rms_min
    }

    public func fetchVADConfig() async throws -> (rms: Double, min: Double, max: Double, step: Double) {
        let env: VADEnvelope = try await requireYRobot().get("/api/audio/vad")
        return (env.vad.rms_min, env.vad.min, env.vad.max, env.vad.step)
    }

    private func requireYRobot() throws -> APIClient {
        guard let yrobotClient else {
            throw APIError.transport("not connected to YRobot dashboard")
        }
        return yrobotClient
    }

    /// Safe Error → String conversion for UI display. Prefer
    /// `localizedDescription`; fall back to `String(describing:)`. Never
    /// use `String(describing: error)` directly on a custom Error
    /// because Swift's default description for a custom Error is
    /// either just the case name ("timeout") or, when bridged through
    /// NSError, "TypeName error N" — neither is what the user wants.
    private static func describe(_ error: any Error) -> String {
        if let localized = error as? LocalizedError, let description = localized.errorDescription {
            return description
        }
        let raw = String(describing: error)
        // The default "TypeName error N" format is useless to operators.
        if raw.hasPrefix("APIError error ") || raw.hasPrefix("URLError error ") {
            return "Request failed. Check that the iPhone is on the same Wi-Fi as the Reachy Mini and the robot is online."
        }
        return raw
    }

    // MARK: - Conversation backend (Stage 3)
    //
    // Two equally-supported backends: XIAOZHI and QWEN. A failure in one
    // must never be hidden behind the other — `running` is sourced from
    // the live runtime, `configured` is sourced from the saved env.
    // `PUT /api/conversation/backend` only flips `restartRequired`; the
    // running backend does not change until the operator explicitly
    // restarts YRobot and the new process rewrites the runtime state.

    /// Reads the current configured + running conversation backend.
    /// Failures throw without clearing the previously observed state, so
    /// transient blips do not cause the UI to flash to "unknown".
    public func refreshConversationBackend() async throws {
        let env: ConversationBackendEnvelope = try await requireYRobot()
            .get("/api/conversation/backend")
        // Fall back to configured when running is missing (e.g. the
        // runtime has not yet written its first state); the UI then
        // shows "configured but not yet observed running".
        let running = env.runningBackend ?? env.configuredBackend
        conversationBackend = ConversationBackendState(
            configured: env.configuredBackend,
            running: running,
            connectionState: env.connectionState,
            lastError: env.error,
            restartRequired: false
        )
    }

    /// Saves a new backend choice. Returns the server's reported
    /// `restart_required` flag. The iOS UI must use it to prompt for
    /// `restartYRobot()` rather than pretending the new backend is live.
    @discardableResult
    public func setConversationBackend(_ backend: ConversationBackend) async throws -> RestartRequirement {
        let resp: ConversationBackendSetResponse = try await requireYRobot()
            .put("/api/conversation/backend", body: ConversationBackendSetRequest(backend: backend))
        if var current = conversationBackend {
            current.configured = resp.configuredBackend
            current.restartRequired = resp.restartRequired
            // running stays as-is — the operator hasn't restarted yet.
            conversationBackend = current
        } else {
            conversationBackend = ConversationBackendState(
                configured: resp.configuredBackend,
                running: resp.configuredBackend,
                connectionState: .notStarted,
                lastError: nil,
                restartRequired: resp.restartRequired
            )
        }
        return resp.restartRequired ? .needsRestart : .none
    }

    /// Reads the QWEN voice picker. Returns nil if not yet fetched.
    public func refreshConversationVoice() async throws {
        let env: ConversationVoiceEnvelope = try await requireYRobot()
            .get("/api/conversation/voice")
        conversationVoice = ConversationVoiceState(
            configured: env.configuredVoice,
            available: env.availableVoices
        )
    }

    /// Saves a QWEN voice choice. Like `setConversationBackend`, only
    /// the `configured` value changes; running is unaffected.
    @discardableResult
    public func setConversationVoice(_ voice: String) async throws -> RestartRequirement {
        let resp: ConversationVoiceSetResponse = try await requireYRobot()
            .put("/api/conversation/voice", body: ConversationVoiceSetRequest(voice: voice))
        if conversationVoice == nil {
            conversationVoice = ConversationVoiceState(
                configured: resp.configuredVoice,
                available: []
            )
        } else if var current = conversationVoice {
            current.configured = resp.configuredVoice
            conversationVoice = current
        }
        return resp.restartRequired ? .needsRestart : .none
    }

    /// Look up the preferred robot from disk and try to connect.
    /// Returns the endpoint used, or nil if there is no preference.
    @discardableResult
    public func restorePreferred() async -> RobotEndpoint? {
        guard let pref = preferences.load() else { return nil }
        let host: RobotEndpoint.Host = pref.lastHost.hasSuffix(".local")
            ? .bonjourName(pref.lastHost)
            : .hostname(pref.lastHost)
        let endpoint = RobotEndpoint(host: host)
        await connect(to: endpoint, hardwareId: pref.hardwareId)
        return endpoint
    }

    /// Try to reach the robot via a manual host (the Reachy mDNS
    /// name by default). Used by the app's launch task so users
    /// who never opened the Network tab still get a working
    /// connection. Returns the result of the connect attempt; nil
    /// if the host could not be parsed at all.
    @discardableResult
    public func tryManualFallback(
        host: String = "reachy-mini.local",
        port: Int = 8042
    ) async -> ConnectionResult? {
        guard let endpoint = RobotEndpointParser.parse(host) else {
            return nil
        }
        return await connect(to: endpoint)
    }
}
