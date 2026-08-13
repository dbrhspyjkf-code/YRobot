import XCTest
@testable import YRobotRemote

final class APIClientTests: XCTestCase, @unchecked Sendable {

    override func setUp() {
        super.setUp()
        StubURLProtocol.handler = nil
    }

    private func makeSession() -> URLSession { makeStubSession() }

    func testGETDecodes() async throws {
        let url = URL(string: "http://robot.local:8042")!
        StubURLProtocol.handler = { req in
            let resp = makeHTTPResponse(req.url!, status: 200)
            let body = #"{"ok":true,"moves":["shake","nod"],"current":null}"#
            return (resp, body.data(using: .utf8))
        }
        let client = APIClient(baseURL: url, session: makeSession())
        let list: MotionList = try await client.get("/api/motion")
        XCTAssertEqual(list.moves, ["shake", "nod"])
        XCTAssertNil(list.current)
    }

    func testPOSTEncodesBody() async throws {
        let url = URL(string: "http://robot.local:8042")!
        let captured = CapturedData()
        let body = #"{"ok":true,"message":"playing","current":"shake"}"#.data(using: .utf8)!
        StubURLProtocol.handler = { req in
            var streamData = Data()
            if let stream = req.httpBodyStream {
                stream.open()
                let bufSize = 4096
                var buf = [UInt8](repeating: 0, count: bufSize)
                while stream.hasBytesAvailable {
                    let read = stream.read(&buf, maxLength: bufSize)
                    if read <= 0 { break }
                    streamData.append(contentsOf: buf.prefix(read))
                }
                stream.close()
            }
            captured.body = streamData
            return (makeHTTPResponse(req.url!, status: 200), body)
        }
        let client = APIClient(baseURL: url, session: makeSession())
        let resp: MotionPlayResponse = try await client.post("/api/motion", body: MotionPlayRequest(move: "shake"))
        XCTAssertEqual(resp.current, "shake")
        let decoded = try JSONDecoder().decode(MotionPlayRequest.self, from: captured.body ?? Data())
        XCTAssertEqual(decoded.move, "shake")
    }

    func testPUT() async throws {
        let url = URL(string: "http://robot.local:8042")!
        StubURLProtocol.handler = { req in
            let resp = makeHTTPResponse(req.url!, status: 200)
            let body = #"{"vad":{"rms_min":0.123,"min":0.001,"max":0.5,"step":0.005,"unit":"RMS"}}"#
            return (resp, body.data(using: .utf8))
        }
        let client = APIClient(baseURL: url, session: makeSession())
        let result: VADEnvelope = try await client.put("/api/audio/vad", body: VADSetRequest(rms_min: 0.123))
        XCTAssertEqual(result.vad.rms_min, 0.123)
        XCTAssertEqual(result.vad.unit, "RMS")
    }

    func testPostDiscardingSucceeds() async throws {
        let url = URL(string: "http://robot.local:8042")!
        StubURLProtocol.handler = { req in
            (makeHTTPResponse(req.url!, status: 200), nil)
        }
        let client = APIClient(baseURL: url, session: makeSession())
        try await client.postDiscarding("/api/system/restart", body: Optional<String>.none)
    }

    func testGetDataReturnsRawBytes() async throws {
        let url = URL(string: "http://robot.local:8042")!
        let jpeg = Data([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10])
        StubURLProtocol.handler = { req in
            let resp = makeHTTPResponse(req.url!, status: 200, headers: ["Content-Type": "image/jpeg"])
            return (resp, jpeg)
        }
        let client = APIClient(baseURL: url, session: makeSession())
        let data = try await client.getData("/api/camera/frame")
        XCTAssertEqual(data, jpeg)
    }

    func test422MapsToValidation() async {
        let url = URL(string: "http://robot.local:8042")!
        StubURLProtocol.handler = { req in
            let resp = makeHTTPResponse(req.url!, status: 422)
            return (resp, #"{"detail":"missing 'move'"}"#.data(using: .utf8))
        }
        let client = APIClient(baseURL: url, session: makeSession())
        do {
            _ = try await client.post("/api/motion", body: MotionPlayRequest(move: "")) as MotionPlayResponse
            XCTFail("Expected validation error")
        } catch let APIError.validation(msg) {
            XCTAssertEqual(msg, "missing 'move'")
        } catch {
            XCTFail("Wrong error: \(error)")
        }
    }

    func test409MapsToRobotBusy() async {
        let url = URL(string: "http://robot.local:8000")!
        StubURLProtocol.handler = { req in
            let resp = makeHTTPResponse(req.url!, status: 409)
            return (resp, #"{"detail":"Another operation is in progress."}"#.data(using: .utf8))
        }
        let client = APIClient(baseURL: url, session: makeSession())
        do {
            _ = try await client.postDiscarding("/wifi/forget", body: WifiForgetRequest(ssid: "Home"))
            XCTFail("Expected robotBusy")
        } catch let APIError.robotBusy(msg) {
            XCTAssertEqual(msg, "Another operation is in progress.")
        } catch {
            XCTFail("Wrong error: \(error)")
        }
    }

    func test400OnSealedConnectMapsToSealedCredential() async {
        let url = URL(string: "http://robot.local:8000")!
        StubURLProtocol.handler = { req in
            let resp = makeHTTPResponse(req.url!, status: 400)
            return (resp, #"{"detail":"decrypt_failed: authentication failed"}"#.data(using: .utf8))
        }
        let client = APIClient(baseURL: url, session: makeSession())
        do {
            _ = try await client.postDiscarding("/wifi/connect_sealed",
                body: WifiSealedConnectRequest(ssid: "X", kid: "K", epk: "E", nonce: "N", ct: "C"))
            XCTFail("Expected sealedCredential")
        } catch let APIError.sealedCredential(msg) {
            XCTAssertTrue(msg.contains("authentication failed"))
        } catch {
            XCTFail("Wrong error: \(error)")
        }
    }

    func test503MapsToUnavailableSubsystem() async {
        let url = URL(string: "http://robot.local:8042")!
        StubURLProtocol.handler = { req in
            let resp = makeHTTPResponse(req.url!, status: 503)
            return (resp, #"{"detail":"amixer not found"}"#.data(using: .utf8))
        }
        let client = APIClient(baseURL: url, session: makeSession())
        do {
            _ = try await client.get("/api/volume") as VolumeEnvelope
            XCTFail("Expected unavailableSubsystem")
        } catch let APIError.unavailableSubsystem(msg) {
            XCTAssertEqual(msg, "amixer not found")
        } catch {
            XCTFail("Wrong error: \(error)")
        }
    }
}
