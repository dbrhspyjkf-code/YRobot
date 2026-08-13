import XCTest
@testable import YRobotRemote

/// Covers the dual-backend model and the save + explicit-restart flow.
///
/// These tests guard two project invariants that matter more than the
/// happy path:
///   1. QWEN failures stay visible; they never collapse into "XIAOZHI
///      connected" on the iOS side.
///   2. Saving a new backend never silently rewrites the running backend;
///      the iOS UI must keep showing "needs restart" until the user
///      explicitly restarts YRobot and the runtime rewrites
///      RUNTIME_HEALTH.backend.
@MainActor
final class ConversationBackendTests: XCTestCase, @unchecked Sendable {

    private let daemonStatusJSON = """
    {"type":"daemon_status","robot_name":"reachy_mini","state":"running",
     "wireless_version":true,"camera_specs_name":"wireless","backend_status":null,
     "wlan_ip":"10.0.0.1","version":"1.9.0","hardware_id":"abc123","face_target":null}
    """

    private let yrobotStatusJSON = """
    {"ok":true,"status":{"service":{"name":"YRobot","state":"active","pid":1,"uptime_s":0},
    "system":{"cpu_percent":1.0,"memory_percent":1.0,"disk_percent":1.0,"temperature_c":40.0,"power":null},
    "daemon":{"available":true,"base_url":"http://127.0.0.1:8000","firmware_version":"1.9.0",
    "hardware_id":"abc123","robot_name":"reachy_mini","daemon_state":"running","motor_mode":"enabled",
    "awake":true,"app_lock_state":"free","active_app":null,"active_app_transport":null,
    "remote_session_active":false,"doa_angle_rad":null,"doa_speech_detected":null,"errors":null},
    "motion":{"ready":true,"thread_alive":true,"mode":"idle","current_move":null,"current_recorded":null,
    "command_queue":0,"loop_hz":50,"deadline_misses":0,"antennas":[0,0],"gaze_target_rad":null,
    "gaze_source":null,"tracking":null},
    "runtime":{"motor_ready":true,"ws_state":"paused","session_id":null,"reconnects":null,
    "tts_active":false,"tts_packets":null,"audio_queue":0,"audio_dropped":null},
    "conversation":{"backend":null,"gateway_url":null,"realtime_mode":null,"tls_verify":null,
    "video_enabled":null,"proactive_enabled":null},
    "audio":{"volume_percent":50,"control":"PCM","range":[0,60],"volume_error":null,"mic":null,
    "input_enabled":false},
    "integrations":{"home_assistant":{"enabled":false,"configured":false,"url":null,"whitelist_path":null},
    "local_info":{"enabled":true}},
    "privacy":{"audio_uploaded_to_gateway":false,"video_uploaded_to_gateway":false,
    "local_media_recording":false},
    "config":{"path":null,"environment_overrides":null}}}
    """

    override func setUp() {
        super.setUp()
        StubURLProtocol.handler = nil
    }

    private func makeSession() -> URLSession { makeStubSession() }

    private func makeRobot() -> RobotSession {
        let prefs = RobotPreferences(defaults: UserDefaults(suiteName: "test.\(UUID().uuidString)")!)
        return RobotSession(preferences: prefs, session: makeSession())
    }

    private func bootBothSubsystemsStub() {
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            if path.contains("/api/daemon/status") {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.daemonStatusJSON.data(using: .utf8))
            }
            if path.contains("/api/status") {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.yrobotStatusJSON.data(using: .utf8))
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
    }

    // MARK: - Reads

    func testRefreshReadsXIAOZHIConnected() async throws {
        bootBothSubsystemsStub()
        let backendJSON = """
        {"configured_backend":"xiaozhi","running_backend":"xiaozhi",
         "connection_state":"connected","error":null}
        """.data(using: .utf8)!
        let session = makeRobot()
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            if path == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.daemonStatusJSON.data(using: .utf8))
            }
            if path == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.yrobotStatusJSON.data(using: .utf8))
            }
            if path == "/api/conversation/backend" {
                return (makeHTTPResponse(req.url!, status: 200), backendJSON)
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        try await session.refreshConversationBackend()

        let state = try XCTUnwrap(session.conversationBackend)
        XCTAssertEqual(state.configured, .xiaozhi)
        XCTAssertEqual(state.running, .xiaozhi)
        XCTAssertEqual(state.connectionState, .connected)
        XCTAssertNil(state.lastError)
        XCTAssertFalse(state.restartRequired)
    }

    func testRefreshReadsQWENConnected() async throws {
        bootBothSubsystemsStub()
        let backendJSON = """
        {"configured_backend":"qwen","running_backend":"qwen",
         "connection_state":"connected","error":null}
        """.data(using: .utf8)!
        let session = makeRobot()
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            if path == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.daemonStatusJSON.data(using: .utf8))
            }
            if path == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.yrobotStatusJSON.data(using: .utf8))
            }
            if path == "/api/conversation/backend" {
                return (makeHTTPResponse(req.url!, status: 200), backendJSON)
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        try await session.refreshConversationBackend()

        let state = try XCTUnwrap(session.conversationBackend)
        XCTAssertEqual(state.configured, .qwen)
        XCTAssertEqual(state.running, .qwen)
        XCTAssertEqual(state.connectionState, .connected)
        XCTAssertNil(state.lastError)
    }

    func testRefreshKeepsQwenWhenQwenFailed() async throws {
        bootBothSubsystemsStub()
        let backendJSON = """
        {"configured_backend":"qwen","running_backend":"qwen",
         "connection_state":"failed","error":"dashscope auth failed"}
        """.data(using: .utf8)!
        let session = makeRobot()
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            if path == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.daemonStatusJSON.data(using: .utf8))
            }
            if path == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.yrobotStatusJSON.data(using: .utf8))
            }
            if path == "/api/conversation/backend" {
                return (makeHTTPResponse(req.url!, status: 200), backendJSON)
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        try await session.refreshConversationBackend()

        let state = try XCTUnwrap(session.conversationBackend)
        XCTAssertEqual(state.running, .qwen,
            "QWEN failure must keep running_backend=qwen, never collapse to xiaozhi")
        XCTAssertEqual(state.connectionState, .failed)
        XCTAssertEqual(state.lastError, "dashscope auth failed")
    }

    // MARK: - Save + restart

    func testSetBackendOnlyFlipsRestartRequired() async throws {
        bootBothSubsystemsStub()
        let before = """
        {"configured_backend":"xiaozhi","running_backend":"xiaozhi",
         "connection_state":"connected","error":null}
        """.data(using: .utf8)!
        let after = """
        {"configured_backend":"qwen","restart_required":true}
        """.data(using: .utf8)!
        let session = makeRobot()
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            let method = req.httpMethod ?? "GET"
            if path == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.daemonStatusJSON.data(using: .utf8))
            }
            if path == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.yrobotStatusJSON.data(using: .utf8))
            }
            if path == "/api/conversation/backend" && method == "GET" {
                return (makeHTTPResponse(req.url!, status: 200), before)
            }
            if path == "/api/conversation/backend" && method == "PUT" {
                return (makeHTTPResponse(req.url!, status: 200), after)
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        try await session.refreshConversationBackend()
        try await session.setConversationBackend(.qwen)

        let state = try XCTUnwrap(session.conversationBackend)
        XCTAssertEqual(state.configured, .qwen, "configured must reflect the saved choice")
        XCTAssertEqual(state.running, .xiaozhi, "running must NOT change without a restart")
        XCTAssertTrue(state.restartRequired, "saving must flag restart_required")
    }

    func testSetBackend422MapsToValidation() async {
        bootBothSubsystemsStub()
        let session = makeRobot()
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            if path == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.daemonStatusJSON.data(using: .utf8))
            }
            if path == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.yrobotStatusJSON.data(using: .utf8))
            }
            if path == "/api/conversation/backend" {
                return (makeHTTPResponse(req.url!, status: 422),
                        #"{"detail":"backend must be 'xiaozhi' or 'qwen'"}"#.data(using: .utf8))
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        do {
            try await session.setConversationBackend(.qwen)
            XCTFail("expected validation error")
        } catch let APIError.validation(msg) {
            XCTAssertEqual(msg, "backend must be 'xiaozhi' or 'qwen'")
        } catch {
            XCTFail("Wrong error: \(error)")
        }
    }

    func testSetBackend503MapsToUnavailableSubsystem() async {
        bootBothSubsystemsStub()
        let session = makeRobot()
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            if path == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.daemonStatusJSON.data(using: .utf8))
            }
            if path == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.yrobotStatusJSON.data(using: .utf8))
            }
            if path == "/api/conversation/backend" {
                return (makeHTTPResponse(req.url!, status: 503),
                        #"{"detail":"could not save backend env: permission denied"}"#.data(using: .utf8))
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        do {
            try await session.setConversationBackend(.qwen)
            XCTFail("expected unavailableSubsystem")
        } catch let APIError.unavailableSubsystem(msg) {
            XCTAssertNotNil(msg)
        } catch {
            XCTFail("Wrong error: \(error)")
        }
    }

    func testRestartDoesNotAutoRerun() async throws {
        // First call to /api/system/restart: timeout (URLError.timedOut).
        // Second call: succeeds. The session must surface the unknown
        // result after the timeout and never auto-retry; the user's
        // explicit "check again" is the only path forward.
        let captured = CapturedInt()
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            let method = req.httpMethod ?? "GET"
            if path == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.daemonStatusJSON.data(using: .utf8))
            }
            if path == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.yrobotStatusJSON.data(using: .utf8))
            }
            if path == "/api/system/restart" && method == "POST" {
                captured.value += 1
                if captured.value == 1 {
                    throw URLError(.timedOut)
                }
                return (makeHTTPResponse(req.url!, status: 200), nil)
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        do {
            try await session.restartYRobot()
            XCTFail("first call should have thrown timeout")
        } catch let APIError.timeout {
            // expected
        } catch {
            XCTFail("Wrong error: \(error)")
        }
        XCTAssertEqual(captured.value, 1, "session must not auto-retry the restart")
    }

    // MARK: - Voice picker

    func testRefreshVoice() async throws {
        bootBothSubsystemsStub()
        let voiceJSON = """
        {"configured_voice":"Ethan","available_voices":["Ethan","Serena","Cherry"]}
        """.data(using: .utf8)!
        let session = makeRobot()
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            if path == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.daemonStatusJSON.data(using: .utf8))
            }
            if path == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.yrobotStatusJSON.data(using: .utf8))
            }
            if path == "/api/conversation/voice" {
                return (makeHTTPResponse(req.url!, status: 200), voiceJSON)
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        try await session.refreshConversationVoice()

        let voice = try XCTUnwrap(session.conversationVoice)
        XCTAssertEqual(voice.configured, "Ethan")
        XCTAssertEqual(voice.available, ["Ethan", "Serena", "Cherry"])
    }

    func testSetVoiceSendsAndReportsRestart() async throws {
        bootBothSubsystemsStub()
        let after = """
        {"configured_voice":"Serena","restart_required":true}
        """.data(using: .utf8)!
        let session = makeRobot()
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            let method = req.httpMethod ?? "GET"
            if path == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.daemonStatusJSON.data(using: .utf8))
            }
            if path == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.yrobotStatusJSON.data(using: .utf8))
            }
            if path == "/api/conversation/voice" && method == "PUT" {
                return (makeHTTPResponse(req.url!, status: 200), after)
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        let restart = try await session.setConversationVoice("Serena")
        XCTAssertEqual(restart, .needsRestart)
    }
}

/// Thread-safe holder for one captured Int (used for call counters).
final class CapturedInt: @unchecked Sendable {
    private let lock = NSLock()
    private var _value: Int = 0
    var value: Int {
        get { lock.withLock { _value } }
        set { lock.withLock { _value = newValue } }
    }
}
