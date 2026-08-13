import XCTest
@testable import YRobotRemote

final class RobotEndpointParserTests: XCTestCase {

    func testEmptyAndWhitespaceRejected() {
        XCTAssertNil(RobotEndpointParser.parseHost(""))
        XCTAssertNil(RobotEndpointParser.parseHost("   "))
        XCTAssertNil(RobotEndpointParser.parse("\n\t"))
    }

    func testIPv4Detected() {
        guard case .ipv4(let s) = RobotEndpointParser.parseHost("192.168.1.14") else {
            XCTFail("expected ipv4"); return
        }
        XCTAssertEqual(s, "192.168.1.14")
    }

    /// The parser is intentionally lenient: anything non-empty that doesn't
    /// look like IPv4 or end in `.local` becomes `.hostname`, and the actual
    /// reachability check happens during the dual-port probe (spec §5.2).
    /// These inputs are accepted as hostnames and will simply fail to connect.
    func testInvalidIPv4FallsThroughToHostname() {
        guard case .hostname = RobotEndpointParser.parseHost("192.168.1.256") else {
            XCTFail("expected hostname"); return
        }
        guard case .hostname = RobotEndpointParser.parseHost("192.168.1") else {
            XCTFail("expected hostname"); return
        }
        guard case .hostname = RobotEndpointParser.parseHost("192.168.1.4.5") else {
            XCTFail("expected hostname"); return
        }
    }

    /// `0192.168.1.4` is not a strictly-valid IPv4 (leading zero on first
    /// octet), but we still accept it as a hostname — the system resolver
    /// will reject it. We do not attempt to be a full IPv4 validator here.
    func testLeadingZeroStillHostname() {
        guard case .hostname = RobotEndpointParser.parseHost("0192.168.1.4") else {
            XCTFail("expected hostname"); return
        }
    }

    func testBonjourLocalDetected() {
        guard case .bonjourName(let s) = RobotEndpointParser.parseHost("reachy-mini.local") else {
            XCTFail("expected bonjourName"); return
        }
        XCTAssertEqual(s, "reachy-mini.local")
    }

    func testOtherNamesAreHostname() {
        guard case .hostname(let s) = RobotEndpointParser.parseHost("reachy_mini") else {
            XCTFail("expected hostname"); return
        }
        XCTAssertEqual(s, "reachy_mini")
    }

    func testEndpointConstructionProducesValidURLs() throws {
        let endpoint = try XCTUnwrap(RobotEndpointParser.parse("192.168.1.14"))
        XCTAssertEqual(try endpoint.daemonBaseURL().absoluteString, "http://192.168.1.14:8000")
        XCTAssertEqual(try endpoint.yrobotBaseURL().absoluteString, "http://192.168.1.14:8042")
    }

    func testInvalidPortRejected() {
        let endpoint = RobotEndpoint(host: .ipv4("10.0.0.1"), daemonPort: 70000, yrobotPort: 8042)
        XCTAssertThrowsError(try endpoint.daemonBaseURL())
    }
}
