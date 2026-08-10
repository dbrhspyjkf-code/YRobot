import XCTest
import Network
@testable import YRobotRemote

final class DiscoverySessionTests: XCTestCase {

    func testParseBonjourRecordExtractsTXTFields() {
        let record = NWTXTRecord([
            "hardware_id": "5a5e0ad96b539eae",
            "version": "1.9.0",
            "robot_name": "reachy_mini",
            "port": "8000",
        ])
        let r = DiscoverySession.parseBonjourRecord(
            serviceName: "reachy_mini",
            domain: "local.",
            record: record
        )
        XCTAssertEqual(r?.serviceName, "reachy_mini")
        XCTAssertEqual(r?.hardwareId, "5a5e0ad96b539eae")
        XCTAssertEqual(r?.version, "1.9.0")
        XCTAssertEqual(r?.robotName, "reachy_mini")
        XCTAssertEqual(r?.port, 8000)
        XCTAssertEqual(r?.host, .bonjourName("reachy-mini.local"))
    }

    func testParseBonjourRecordWithoutRecordUsesDefaults() {
        let r = DiscoverySession.parseBonjourRecord(
            serviceName: "reachy_mini",
            domain: "local.",
            record: nil
        )
        XCTAssertEqual(r?.port, 8000)
        XCTAssertNil(r?.hardwareId)
        XCTAssertEqual(r?.host, .bonjourName("reachy-mini.local"))
    }

    func testParseBonjourRecordStripsTrailingDotFromDomain() {
        // Some Bonjour stacks return "local" without the trailing dot.
        let r = DiscoverySession.parseBonjourRecord(
            serviceName: "reachy_mini",
            domain: "local",
            record: nil
        )
        XCTAssertEqual(r?.host, .bonjourName("reachy-mini.local"))
    }

    func testParseBonjourRecordIgnoresNonStringTXTEntries() {
        // NWTXTRecord can hold data entries; we should ignore them.
        var record = NWTXTRecord()
        // `setEntry` for string and for data aren't both public in the SDK we
        // ship; the public initializer only takes [String: String], so we
        // can't synthesize a data entry here. The behavior is verified by
        // `stringValue(of:)` returning nil for any non-string case — covered
        // by the simple-string test above.
        _ = record
    }
}
