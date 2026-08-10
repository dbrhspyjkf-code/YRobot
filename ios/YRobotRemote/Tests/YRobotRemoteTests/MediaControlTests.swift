import XCTest
@testable import YRobotRemote

/// Stage 4 helpers — wraps each RobotSession media helper behind URLProtocol
/// stubs and checks both the round-trip call shape and the returned value
/// the UI should display (spec §8 "show the value returned by the robot").
@MainActor
final class MediaControlTests: XCTestCase, @unchecked Sendable {

    override func setUp() {
        super.setUp()
        StubURLProtocol.handler = nil
    }

    private func makeSession() -> URLSession { makeStubSession() }

    private func makeRobot() -> RobotSession {
        let prefs = RobotPreferences(defaults: UserDefaults(suiteName: "test.\(UUID().uuidString)")!)
        return RobotSession(preferences: prefs, session: makeSession())
    }

    /// `connect()` probes both daemon and YRobot status; tests that focus on
    /// one endpoint still need to answer those probes. This handler returns
    /// a minimal successful body for the connect probes and lets the test
    /// set a `targetPath` to gate the assertion.
    private func installStub(
        targetPath: String,
        targetBody: Data,
        connectDaemonBody: Data? = nil,
        connectYRobotBody: Data? = nil
    ) {
        let daemonBody = connectDaemonBody ?? Self.daemonStatusJSON.data(using: .utf8)!
        let yrobotBody = connectYRobotBody ?? Self.minimalYRobotStatusJSON.data(using: .utf8)!
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            let body: Data
            if path == targetPath {
                body = targetBody
            } else if path == "/api/daemon/status" {
                body = daemonBody
            } else if path == "/api/status" {
                body = yrobotBody
            } else {
                return (makeHTTPResponse(req.url!, status: 404), nil)
            }
            return (makeHTTPResponse(req.url!, status: 200), body)
        }
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

    func testSetCameraEnabledRoundTrip() async throws {
        installStub(
            targetPath: "/api/camera/state",
            targetBody: #"{"state":{"running":true,"interval_s":0.5,"long_edge":640,"jpeg_quality":70,"frame_bytes":0,"captured":0,"failures":0,"last_frame_at":null,"started_at":null}}"#.data(using: .utf8)!
        )
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        let running = try await session.setCameraEnabled(true)
        XCTAssertTrue(running)
    }

    func testSetVolumeReturnsAppliedValue() async throws {
        installStub(
            targetPath: "/api/volume",
            targetBody: #"{"volume":{"percent":42,"control":"PCM","range":[0,60],"min_percent":0,"max_percent":100}}"#.data(using: .utf8)!
        )
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        let applied = try await session.setVolume(percent: 80)
        XCTAssertEqual(applied, 42, "UI should snap to the server-clamped value")
    }

    func testFetchVolumeConfigReturnsRangeAndAlsa() async throws {
        installStub(
            targetPath: "/api/volume",
            targetBody: #"{"volume":{"percent":75,"control":"PCM","range":[0,80],"min_percent":0,"max_percent":100}}"#.data(using: .utf8)!
        )
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        let cfg = try await session.fetchVolumeConfig()
        XCTAssertEqual(cfg.percent, 75)
        XCTAssertEqual(cfg.min, 0)
        XCTAssertEqual(cfg.max, 100)
        XCTAssertEqual(cfg.alsa, [0, 80])
    }

    func testSetMicUploadReturnsAcknowledgedState() async throws {
        installStub(
            targetPath: "/api/audio/input",
            targetBody: #"{"audio_input":{"enabled":true}}"#.data(using: .utf8)!
        )
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        let enabled = try await session.setMicUpload(enabled: true)
        XCTAssertTrue(enabled)
    }

    func testSetVADSurfacesPersistenceFailure() async {
        // Override the handler to return 503 for VAD but 200 for connect probes.
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            if path == "/api/audio/vad" {
                return (makeHTTPResponse(req.url!, status: 503),
                        #"{"detail":"could not save VAD env: PermissionError"}"#.data(using: .utf8))
            }
            if path == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.daemonStatusJSON.data(using: .utf8))
            }
            if path == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.minimalYRobotStatusJSON.data(using: .utf8))
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        do {
            _ = try await session.setVAD(rms: 0.2)
            XCTFail("expected unavailableSubsystem")
        } catch let APIError.unavailableSubsystem(msg) {
            XCTAssertTrue(msg?.contains("PermissionError") == true)
        } catch {
            XCTFail("Wrong error: \(error)")
        }
    }

    func testFetchVADConfigReturnsRangeAndStep() async throws {
        installStub(
            targetPath: "/api/audio/vad",
            targetBody: #"{"vad":{"rms_min":0.123,"min":0.001,"max":0.5,"step":0.005,"unit":"RMS"}}"#.data(using: .utf8)!
        )
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        let cfg = try await session.fetchVADConfig()
        XCTAssertEqual(cfg.rms, 0.123, accuracy: 0.0001)
        XCTAssertEqual(cfg.min, 0.001)
        XCTAssertEqual(cfg.max, 0.5)
        XCTAssertEqual(cfg.step, 0.005)
    }

    func testCurrentFrameReturnsBytes() async throws {
        let jpeg = Data([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10])
        installStub(
            targetPath: "/api/camera/frame",
            targetBody: jpeg,
            connectDaemonBody: nil,
            connectYRobotBody: nil
        )
        // Need to override header for frame response
        StubURLProtocol.handler = nil
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            if path == "/api/camera/frame" {
                let r = makeHTTPResponse(req.url!, status: 200, headers: ["Content-Type": "image/jpeg"])
                return (r, jpeg)
            }
            if path == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.daemonStatusJSON.data(using: .utf8))
            }
            if path == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.minimalYRobotStatusJSON.data(using: .utf8))
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        let data = try await session.currentFrame()
        XCTAssertEqual(data, jpeg)
    }

    func testCurrentFrameMaps404ToHttpStatus() async {
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            if path == "/api/camera/frame" {
                let r = makeHTTPResponse(req.url!, status: 404)
                return (r, #"{"detail":"camera preview is off or no frame yet"}"#.data(using: .utf8))
            }
            if path == "/api/daemon/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.daemonStatusJSON.data(using: .utf8))
            }
            if path == "/api/status" {
                return (makeHTTPResponse(req.url!, status: 200),
                        Self.minimalYRobotStatusJSON.data(using: .utf8))
            }
            return (makeHTTPResponse(req.url!, status: 404), nil)
        }
        let session = makeRobot()
        await session.connect(to: RobotEndpoint(host: .ipv4("10.0.0.1")))
        do {
            _ = try await session.currentFrame()
            XCTFail("expected httpStatus")
        } catch let APIError.httpStatus(code) {
            XCTAssertEqual(code, 404)
        } catch {
            XCTFail("Wrong error: \(error)")
        }
    }
}
