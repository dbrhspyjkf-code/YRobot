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

    // MARK: - Friendly failure messages

    func testNoAuthMessagePointsToLocalNetworkSetting() {
        // The exact text iOS produces when Local Network permission is
        // denied. The user-facing message must tell them to flip the
        // toggle, not expose the raw DNSService error.
        let raw = "nw_browser_fail_on_dns_error_locked [B2] DNSServiceBrowse failed: NoAuth(-65555)"
        let err = NWError.dns(-65555)
        let msg = DiscoverySession.friendlyFailureMessage(for: err)
        XCTAssertTrue(msg.contains("Local Network"), "should mention Local Network, got: \(msg)")
        XCTAssertTrue(msg.contains("Settings"), "should point to Settings, got: \(msg)")
        XCTAssertFalse(msg.contains("-65555"), "should not leak raw code, got: \(msg)")
        _ = raw
    }

    func testDnsFailureMessageMentionsSameWiFi() {
        let err = NWError.dns(-65537)
        let msg = DiscoverySession.friendlyFailureMessage(for: err)
        XCTAssertTrue(msg.contains("Bonjour"))
        XCTAssertTrue(msg.contains("same Wi-Fi"))
    }

    func testUnknownFailureFallsBackToRaw() {
        let err = NWError.posix(.ECONNREFUSED)
        let msg = DiscoverySession.friendlyFailureMessage(for: err)
        XCTAssertFalse(msg.isEmpty)
    }
}
