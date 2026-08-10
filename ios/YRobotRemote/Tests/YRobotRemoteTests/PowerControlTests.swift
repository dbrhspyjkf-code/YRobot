import XCTest
@testable import YRobotRemote

/// Verifies the Stage 5 power / daemon / logs / motion helpers wrap the
/// right endpoints and surface the right errors. The actual destructive
/// actions (reboot / poweroff) are not exercised here — they terminate the
/// host process — but their request shape is asserted so the live
/// confirmation dialog targets the correct body.
@MainActor
final class PowerControlTests: XCTestCase, @unchecked Sendable {

    override func setUp() {
        super.setUp()
        StubURLProtocol.handler = nil
    }

    private func makeSession() -> URLSession { makeStubSession() }

    private func makeRobot() -> RobotSession {
        let prefs = RobotPreferences(defaults: UserDefaults(suiteName: "test.\(UUID().uuidString)")!)
        return RobotSession(preferences: prefs, session: makeSession())
    }

    nonisolated(unsafe) private static let daemonStatusJSON = """
    {"type":"daemon_status","robot_name":"reachy_mini","state":"running",
     "wireless_version":true,"camera_specs_name":"wireless","backend_status":null,
     "wlan_ip":"10.0.0.1","version":"1.9.0","hardware_id":"abc123","face_target":null}
    """

    nonisolated(unsafe) private static let minimalYRobotStatusJSON = """
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

    private func pathStub(path: String, status: Int = 200, body: String) {
        StubURLProtocol.handler = { req in
            let p = req.url?.path ?? ""
            if p == path {
                return (makeHTTPResponse(req.url!, status: status),
                        body.data(using: .utf8))
            }
            if p == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.daemonStatusJSON.data(using: .utf8))
            }
            if p == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.minimalYRobotStatusJSON.data(using: .utf8))
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
    }

    func testWakePostsToDaemonAction() async throws {
        let captured = CapturedData()
        StubURLProtocol.handler = { req in
            captured.body = req.httpBodyStream.flatMap { Self.drain($0) }
            let p = req.url?.path ?? ""
            if p == "/api/reachy-daemon/action" {
                return (makeHTTPResponse(req.url!, status: 200),
                        #"{"ok":true,"action":"wake"}"#.data(using: .utf8))
            }
            if p == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.daemonStatusJSON.data(using: .utf8))
            }
            if p == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.minimalYRobotStatusJSON.data(using: .utf8))
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        _ = try await session.sendDaemonAction(.wake)
        XCTAssertNotNil(captured.body)
        let decoded = try JSONDecoder().decode(ReachyDaemonActionRequest.self, from: captured.body ?? Data())
        XCTAssertEqual(decoded.action, .wake)
    }

    func testSleepPostsToDaemonAction() async throws {
        StubURLProtocol.handler = { req in
            let p = req.url?.path ?? ""
            if p == "/api/reachy-daemon/action" {
                return (makeHTTPResponse(req.url!, status: 200),
                        #"{"ok":true,"action":"sleep"}"#.data(using: .utf8))
            }
            if p == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.daemonStatusJSON.data(using: .utf8))
            }
            if p == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.minimalYRobotStatusJSON.data(using: .utf8))
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        _ = try await session.sendDaemonAction(.sleep)
    }

    func testRestartDaemonPostsToDaemonAction() async throws {
        pathStub(path: "/api/reachy-daemon/action",
                 body: #"{"ok":true,"action":"restart"}"#)
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        _ = try await session.sendDaemonAction(.restart)
    }

    func testRestartYRobotPostsNoBody() async throws {
        let method = CapturedString()
        let path = CapturedString()
        StubURLProtocol.handler = { req in
            method.value = req.httpMethod
            path.value = req.url?.path
            let p = req.url?.path ?? ""
            if p == "/api/system/restart" {
                return (makeHTTPResponse(req.url!, status: 200), nil)
            }
            if p == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.daemonStatusJSON.data(using: .utf8))
            }
            if p == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.minimalYRobotStatusJSON.data(using: .utf8))
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        try await session.restartYRobot()
        XCTAssertEqual(method.value, "POST")
        XCTAssertEqual(path.value, "/api/system/restart")
    }

    func testRebootPostsPowerBody() async throws {
        let captured = CapturedData()
        StubURLProtocol.handler = { req in
            captured.body = req.httpBodyStream.flatMap { Self.drain($0) }
            let p = req.url?.path ?? ""
            if p == "/api/system/power" {
                return (makeHTTPResponse(req.url!, status: 200), nil)
            }
            if p == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.daemonStatusJSON.data(using: .utf8))
            }
            if p == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.minimalYRobotStatusJSON.data(using: .utf8))
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        try await session.systemPower(.reboot)
        let decoded = try JSONDecoder().decode(SystemPowerRequest.self, from: captured.body ?? Data())
        XCTAssertEqual(decoded.action, .reboot)
    }

    func testPowerOffPostsPowerBody() async throws {
        let captured = CapturedData()
        StubURLProtocol.handler = { req in
            captured.body = req.httpBodyStream.flatMap { Self.drain($0) }
            let p = req.url?.path ?? ""
            if p == "/api/system/power" {
                return (makeHTTPResponse(req.url!, status: 200), nil)
            }
            if p == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.daemonStatusJSON.data(using: .utf8))
            }
            if p == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.minimalYRobotStatusJSON.data(using: .utf8))
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        try await session.systemPower(.poweroff)
        let decoded = try JSONDecoder().decode(SystemPowerRequest.self, from: captured.body ?? Data())
        XCTAssertEqual(decoded.action, .poweroff)
    }

    func testPlayMotionSendsMoveName() async throws {
        StubURLProtocol.handler = { req in
            let p = req.url?.path ?? ""
            if p == "/api/motion" {
                return (makeHTTPResponse(req.url!, status: 200),
                        #"{"ok":true,"message":"playing","current":"dance1"}"#.data(using: .utf8))
            }
            if p == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.daemonStatusJSON.data(using: .utf8))
            }
            if p == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.minimalYRobotStatusJSON.data(using: .utf8))
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        let resp = try await session.playMotion(name: "dance1")
        XCTAssertEqual(resp.current, "dance1")
    }

    func testPlayMotion422MapsToValidation() async {
        StubURLProtocol.handler = { req in
            let p = req.url?.path ?? ""
            if p == "/api/motion" {
                return (makeHTTPResponse(req.url!, status: 422),
                        #"{"detail":"missing 'move'"}"#.data(using: .utf8))
            }
            if p == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.daemonStatusJSON.data(using: .utf8))
            }
            if p == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.minimalYRobotStatusJSON.data(using: .utf8))
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        do {
            _ = try await session.playMotion(name: "")
            XCTFail("expected validation")
        } catch let APIError.validation(msg) {
            XCTAssertEqual(msg, "missing 'move'")
        } catch {
            XCTFail("Wrong error: \(error)")
        }
    }

    func testFetchLogsWithChatFilter() async throws {
        let capturedQuery = CapturedQueryItems()
        StubURLProtocol.handler = { req in
            capturedQuery.items = req.url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false)?.queryItems } ?? []
            let p = req.url?.path ?? ""
            if p == "/api/logs" {
                return (makeHTTPResponse(req.url!, status: 200),
                        #"{"unit":"yrobot.service","level":"info","filter":"chat","lines":0,"logs":[]}"#.data(using: .utf8))
            }
            if p == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.daemonStatusJSON.data(using: .utf8))
            }
            if p == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.minimalYRobotStatusJSON.data(using: .utf8))
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        _ = try await session.fetchLogs(minLevel: .info, kind: .chat, lines: 50)
        XCTAssertTrue(capturedQuery.items.contains(URLQueryItem(name: "filter", value: "chat")))
        XCTAssertTrue(capturedQuery.items.contains(URLQueryItem(name: "lines", value: "50")))
        XCTAssertTrue(capturedQuery.items.contains(URLQueryItem(name: "min_level", value: "info")))
    }

    nonisolated static func drain(_ stream: InputStream) -> Data? {
        stream.open()
        defer { stream.close() }
        var out = Data()
        let bufSize = 4096
        var buf = [UInt8](repeating: 0, count: bufSize)
        while stream.hasBytesAvailable {
            let read = stream.read(&buf, maxLength: bufSize)
            if read <= 0 { break }
            out.append(contentsOf: buf.prefix(read))
        }
        return out
    }
}

/// Thread-safe holder for a captured String from a URLProtocol stub.
final class CapturedString: @unchecked Sendable {
    private let lock = NSLock()
    private var _value: String?
    var value: String? {
        get { lock.withLock { _value } }
        set { lock.withLock { _value = newValue } }
    }
}

final class CapturedQueryItems: @unchecked Sendable {
    private let lock = NSLock()
    private var _items: [URLQueryItem] = []
    var items: [URLQueryItem] {
        get { lock.withLock { _items } }
        set { lock.withLock { _items = newValue } }
    }
}
