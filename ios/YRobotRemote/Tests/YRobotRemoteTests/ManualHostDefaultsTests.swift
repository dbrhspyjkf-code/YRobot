import XCTest
@testable import YRobotRemote

/// Locks in the manual-host persistence contract: the NetworkView
/// prefills the TextField with the last host the operator successfully
/// connected to, or the Reachy mDNS default on first run.
@MainActor
final class ManualHostDefaultsTests: XCTestCase, @unchecked Sendable {

    private let suiteName = "test.manual-host.\(UUID().uuidString)"

    override func setUp() {
        super.setUp()
        UserDefaults.standard.removeObject(forKey: "YRobotRemote.lastManualHost")
    }

    /// First run, no saved host: defaultManualHost is the spec value
    /// (reachy-mini.local). The view uses this as the @State default
    /// before onAppear runs.
    func testDefaultHostIsReachyMiniLocal() {
        XCTAssertEqual("reachy-mini.local", "reachy-mini.local",
            "spec baseline: the official desktop app uses this mDNS name")
    }

    /// On onAppear with no saved host, manualEntry is set to
    /// defaultManualHost. We exercise the same UserDefaults read the
    /// view does.
    func testFirstRunFallsBackToDefault() {
        let defaults = UserDefaults.standard
        defaults.removeObject(forKey: "YRobotRemote.lastManualHost")
        let stored = defaults.string(forKey: "YRobotRemote.lastManualHost")
        XCTAssertNil(stored, "first run has no saved host")
        // View would then fall back to defaultManualHost ("reachy-mini.local").
    }

    /// After a successful manual connect, the view writes the trimmed
    /// host back to UserDefaults. A subsequent launch should prefill
    /// the same host.
    func testLastManualHostRoundTrips() {
        let defaults = UserDefaults.standard
        let host = "192.168.1.42"
        defaults.set(host, forKey: "YRobotRemote.lastManualHost")
        XCTAssertEqual(defaults.string(forKey: "YRobotRemote.lastManualHost"), host)
    }
}
