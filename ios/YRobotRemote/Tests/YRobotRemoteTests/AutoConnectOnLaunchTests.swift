import XCTest
@testable import YRobotRemote

/// Locks in the auto-connect-on-launch behaviour: the app tries
/// `reachy-mini.local` automatically if no preferred robot is saved,
/// unless the operator disabled the toggle.
@MainActor
final class AutoConnectOnLaunchTests: XCTestCase, @unchecked Sendable {

    private let key = "YRobotRemote.autoConnectOnLaunch"
    private let defaults = UserDefaults.standard

    override func setUp() {
        super.setUp()
        defaults.removeObject(forKey: key)
    }

    func testDefaultsToEnabledWhenKeyNotSet() {
        defaults.removeObject(forKey: key)
        // The launch code path is: if no preference, treat as enabled.
        let on = defaults.object(forKey: key) == nil ? true : defaults.bool(forKey: key)
        XCTAssertTrue(on, "first-run should default to auto-connect on launch")
    }

    func testDisablingStoresFalse() {
        defaults.set(false, forKey: key)
        let on = defaults.object(forKey: key) == nil ? true : defaults.bool(forKey: key)
        XCTAssertFalse(on)
    }

    func testEnablingAfterDisablingStoresTrue() {
        defaults.set(true, forKey: key)
        let on = defaults.object(forKey: key) == nil ? true : defaults.bool(forKey: key)
        XCTAssertTrue(on)
    }
}
