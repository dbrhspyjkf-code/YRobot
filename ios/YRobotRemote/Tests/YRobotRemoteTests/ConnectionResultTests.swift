import XCTest
@testable import YRobotRemote

/// Verifies the four-state connection model and the bounded rediscovery
/// behavior. These cases are at the Session level — we construct a
/// `RobotSession` against a stub URLProtocol and assert on what
/// `connect()` and `rediscover()` return.
@MainActor
final class ConnectionResultTests: XCTestCase, @unchecked Sendable {

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

    // MARK: - Four-state result

    func testConnectReturnsBothAvailableWhenBothPortsAnswer() async {
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
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        let session = makeRobot()
        let result = await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))

        XCTAssertEqual(result, .bothAvailable)
    }

    func testConnectReturnsDaemonOnlyWhenYRobotUnavailable() async {
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            if path == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.daemonStatusJSON.data(using: .utf8))
            }
            if path == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 503),
                        #"{"detail":"dashboard down"}"#.data(using: .utf8))
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        let session = makeRobot()
        let result = await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))

        guard case .daemonOnly = result else {
            XCTFail("expected .daemonOnly, got \(result)")
            return
        }
        XCTAssertTrue(result.isConnected)
        XCTAssertNotNil(result.subsystemFailureMessage)
    }

    func testConnectReturnsYRobotOnlyWhenDaemonUnavailable() async {
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            if path == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 503),
                        #"{"detail":"daemon down"}"#.data(using: .utf8))
            }
            if path == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        self.yrobotStatusJSON.data(using: .utf8))
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        let session = makeRobot()
        let result = await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))

        guard case .yrobotOnly = result else {
            XCTFail("expected .yrobotOnly, got \(result)")
            return
        }
        XCTAssertTrue(result.isConnected)
    }

    func testConnectReturnsUnavailableWhenBothPortsFail() async {
        StubURLProtocol.handler = { req in
            return (makeHTTPResponse(req.url!, status: 500), nil)
        }
        let session = makeRobot()
        let result = await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))

        guard case .unavailable = result else {
            XCTFail("expected .unavailable, got \(result)")
            return
        }
        XCTAssertFalse(result.isConnected)
    }

    /// Bug fix: when both probes fail with a transport error, the
    /// subsystem reason used to be "APIError error 0" — the NSError-
    /// bridged default description that says nothing to a real user.
    /// The Session must surface a human-readable reason instead.
    func testSubsystemReasonIsHumanReadable() async {
        StubURLProtocol.handler = { req in
            // Force URLError.cannotConnectToHost by making the
            // URLSession throw before we get a response.
            throw URLError(.cannotConnectToHost)
        }
        let session = makeRobot()
        _ = await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))

        if case .unavailable(let reason, _) = session.availability.daemon {
            XCTAssertFalse(reason.hasPrefix("APIError error"),
                "subsystem reason must not be a raw NSError bridge; got '\(reason)'")
        } else {
            XCTFail("expected daemon to be .unavailable, got \(session.availability.daemon)")
        }
    }

    // MARK: - Manual entry does not save on full failure

    func testManualEntryWithoutEndpointIsRejected() {
        // The RobotEndpoint parser already rejects empty input; this test
        // documents the user-facing contract: empty input must not
        // create a phantom endpoint.
        let parsed = RobotEndpointParser.parse("   ")
        XCTAssertNil(parsed)
    }

    // MARK: - Bounded rediscovery

    func testRediscoverGivesUpAfterMaxAttempts() async {
        // First connect: both ports fail -> unavailable, endpoint is still
        // saved. Then rediscover with all attempts failing.
        StubURLProtocol.handler = { req in
            return (makeHTTPResponse(req.url!, status: 503), nil)
        }
        let session = makeRobot()
        _ = await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))

        let outcome = await session.rediscover(maxAttempts: 3, baseDelay: 0.0)
        XCTAssertEqual(outcome, .exhausted)
        XCTAssertFalse(session.availability.bothAvailable)
    }

    func testRediscoverSucceedsWhenRobotAppearsMidLoop() async {
        // First connect: both ports fail. Then rediscover; on the second
        // attempt both ports start answering.
        let attempts = CapturedInt()
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            if path == "/api/daemon/status" || path == "/api/status" {
                attempts.value += 1
                if attempts.value >= 3 {
                    if path == "/api/daemon/status" {
                        return (makeHTTPResponse(req.url!, status: 200),
                                self.daemonStatusJSON.data(using: .utf8))
                    }
                    return (makeHTTPResponse(req.url!, status: 200),
                            self.yrobotStatusJSON.data(using: .utf8))
                }
            }
            return (makeHTTPResponse(req.url!, status: 503), nil)
        }
        let session = makeRobot()
        _ = await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))

        let outcome = await session.rediscover(maxAttempts: 5, baseDelay: 0.0)
        XCTAssertEqual(outcome, .connected)
        XCTAssertTrue(session.availability.bothAvailable)
    }
}
