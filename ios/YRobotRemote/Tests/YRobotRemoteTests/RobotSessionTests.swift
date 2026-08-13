import XCTest
@testable import YRobotRemote

/// Reuses the StubURLProtocol pattern from APIClientTests by spinning up two
/// APIClient-bearing sessions pointing at loopback URLs. RobotSession's URL
/// construction is exercised by passing an endpoint with .ipv4 hosts.
@MainActor
final class RobotSessionTests: XCTestCase, @unchecked Sendable {

    private var daemonURL: URL { URL(string: "http://10.0.0.1:8000")! }
    private var yrobotURL: URL { URL(string: "http://10.0.0.1:8042")! }

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

    private func makeRobotSession() -> RobotSession {
        let prefs = RobotPreferences(defaults: UserDefaults(suiteName: "test.\(UUID().uuidString)")!)
        return RobotSession(preferences: prefs, session: makeSession())
    }

    func testBothSubsystemsAvailable() async {
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            let (resp, body): (HTTPURLResponse, Data?)
            if path.contains("/api/daemon/status") {
                resp = makeHTTPResponse(req.url!, status: 200)
                body = self.daemonStatusJSON.data(using: .utf8)
            } else if path.contains("/api/status") {
                resp = makeHTTPResponse(req.url!, status: 200)
                body = self.yrobotStatusJSON.data(using: .utf8)
            } else {
                resp = makeHTTPResponse(req.url!, status: 404, headers: [:])
                body = nil
            }
            return (resp, body)
        }
        let session = makeRobotSession()
        let result = await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        XCTAssertEqual(result, .bothAvailable)
        XCTAssertTrue(session.availability.bothAvailable)
        XCTAssertNotNil(session.status)
        XCTAssertNotNil(session.daemonStatus)
    }

    func testPartialFailureDaemonOnly() async {
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            if path.contains("/api/daemon/status") {
                let r = makeHTTPResponse(req.url!, status: 200)
                return (r, self.daemonStatusJSON.data(using: .utf8))
            }
            let r = makeHTTPResponse(req.url!, status: 503)
            return (r, #"{"detail":"down"}"#.data(using: .utf8))
        }
        let session = makeRobotSession()
        let result = await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        XCTAssertTrue(result.isConnected, "connect() returns a connected result when at least one port answered")
        XCTAssertEqual(session.availability.daemon, .available)
        if case .unavailable = session.availability.yrobot {} else {
            XCTFail("yrobot should be unavailable")
        }
        XCTAssertNotNil(session.daemonStatus)
        XCTAssertNil(session.status)
    }

    func testNoEndpointNoProbes() async {
        let session = makeRobotSession()
        await session.refresh()
        XCTAssertEqual(session.availability.daemon, .unknown)
        XCTAssertEqual(session.availability.yrobot, .unknown)
    }

    func testRestorePreferredReconnects() async {
        let suite = UserDefaults(suiteName: "test.\(UUID().uuidString)")!
        let prefs = RobotPreferences(defaults: suite)
        prefs.save(.init(serviceName: "reachy_mini", hardwareId: "abc", lastHost: "reachy-mini.local"))

        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            if path.contains("/api/daemon/status") {
                let r = makeHTTPResponse(req.url!, status: 200)
                return (r, self.daemonStatusJSON.data(using: .utf8))
            }
            let r = makeHTTPResponse(req.url!, status: 200)
            return (r, self.yrobotStatusJSON.data(using: .utf8))
        }
        let session = RobotSession(preferences: prefs, session: makeSession())
        let endpoint = await session.restorePreferred()
        XCTAssertNotNil(endpoint)
        XCTAssertEqual(session.hardwareId, "abc")
    }

    func testDisconnectClearsState() async {
        StubURLProtocol.handler = { req in
            let r = makeHTTPResponse(req.url!, status: 200)
            return (r, self.daemonStatusJSON.data(using: .utf8))
        }
        let session = makeRobotSession()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")), hardwareId: "abc")
        XCTAssertNotNil(session.endpoint)
        session.disconnect()
        XCTAssertNil(session.endpoint)
        XCTAssertNil(session.status)
        XCTAssertEqual(session.availability, RobotSession.Availability())
    }
}
