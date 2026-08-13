import Foundation

/// The two conversation backends that YRobot can run.
///
/// XIAOZHI and QWEN are independent, equally-supported modes. A failure in
/// one is never silently swapped for the other — the iOS UI must keep
/// showing whichever backend is currently running, even if it is in a
/// failed state, so the operator can tell the difference between
/// "XIAOZHI connected" and "QWEN failed".
public enum ConversationBackend: String, Codable, Sendable, Equatable, CaseIterable, Hashable {
    case xiaozhi
    case qwen
}

/// Connection state for a conversation backend, mirroring the values the
/// YRobot runtime reports through `RUNTIME_HEALTH.snapshot()`.
///
/// The values are deliberately coarse — the iOS UI only needs enough
/// information to decide between "ready to talk", "reconnecting",
/// "paused", "failed". Anything finer-grained lives in the dashboard logs.
public enum ConversationConnectionState: String, Codable, Sendable, Equatable, Hashable {
    case notStarted = "not_started"
    case connecting
    case connected
    case paused
    case reconnecting
    case failed
    case safeMode = "safe_mode"
}

/// Save-side effect reported by the dashboard.
///
/// Some configuration changes only take effect after YRobot restarts; the
/// iOS UI uses this to decide whether to show a "needs restart" prompt
/// instead of claiming the new value is already live.
public enum RestartRequirement: String, Codable, Sendable, Equatable, Hashable {
    case none
    case needsRestart = "needs_restart"
}

/// Combined view of the conversation backend as the iOS app sees it.
///
/// `configured` is what the operator chose and saved. `running` is what
/// the robot is actually doing right now. They diverge while YRobot still
/// runs the previous process; the UI must keep that distinction visible
/// rather than collapsing them.
public struct ConversationBackendState: Codable, Sendable, Equatable {
    public var configured: ConversationBackend
    public var running: ConversationBackend
    public var connectionState: ConversationConnectionState
    public var lastError: String?
    public var restartRequired: Bool

    public init(
        configured: ConversationBackend,
        running: ConversationBackend,
        connectionState: ConversationConnectionState,
        lastError: String? = nil,
        restartRequired: Bool = false
    ) {
        self.configured = configured
        self.running = running
        self.connectionState = connectionState
        self.lastError = lastError
        self.restartRequired = restartRequired
    }
}

/// Voice picker state for the QWEN backend.
public struct ConversationVoiceState: Codable, Sendable, Equatable {
    public var configured: String
    public var available: [String]

    public init(configured: String, available: [String]) {
        self.configured = configured
        self.available = available
    }
}

// MARK: - Wire models

/// `GET /api/conversation/backend` response.
public struct ConversationBackendEnvelope: Codable, Sendable, Equatable {
    public let configuredBackend: ConversationBackend
    public let runningBackend: ConversationBackend?
    public let connectionState: ConversationConnectionState
    public let error: String?

    enum CodingKeys: String, CodingKey {
        case configuredBackend = "configured_backend"
        case runningBackend = "running_backend"
        case connectionState = "connection_state"
        case error
    }
}

/// `PUT /api/conversation/backend` request and response.
public struct ConversationBackendSetRequest: Codable, Sendable, Equatable {
    public let backend: ConversationBackend
    public init(backend: ConversationBackend) { self.backend = backend }
}

public struct ConversationBackendSetResponse: Codable, Sendable, Equatable {
    public let configuredBackend: ConversationBackend
    public let restartRequired: Bool

    enum CodingKeys: String, CodingKey {
        case configuredBackend = "configured_backend"
        case restartRequired = "restart_required"
    }
}

/// `GET /api/conversation/voice` response.
public struct ConversationVoiceEnvelope: Codable, Sendable, Equatable {
    public let configuredVoice: String
    public let availableVoices: [String]

    enum CodingKeys: String, CodingKey {
        case configuredVoice = "configured_voice"
        case availableVoices = "available_voices"
    }
}

/// `PUT /api/conversation/voice` request and response.
public struct ConversationVoiceSetRequest: Codable, Sendable, Equatable {
    public let voice: String
    public init(voice: String) { self.voice = voice }
}

public struct ConversationVoiceSetResponse: Codable, Sendable, Equatable {
    public let configuredVoice: String
    public let restartRequired: Bool

    enum CodingKeys: String, CodingKey {
        case configuredVoice = "configured_voice"
        case restartRequired = "restart_required"
    }
}
