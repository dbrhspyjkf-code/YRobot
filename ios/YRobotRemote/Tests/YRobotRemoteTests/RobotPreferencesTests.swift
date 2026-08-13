import XCTest
@testable import YRobotRemote

final class RobotPreferencesTests: XCTestCase {

    private func makePrefs() -> (RobotPreferences, UserDefaults) {
        let suite = UserDefaults(suiteName: "test.\(UUID().uuidString)")!
        return (RobotPreferences(defaults: suite), suite)
    }

    func testLoadReturnsNilWhenEmpty() {
        let (prefs, _) = makePrefs()
        XCTAssertNil(prefs.load())
    }

    func testSaveLoadRoundTrip() {
        let (prefs, _) = makePrefs()
        let robot = RobotPreferences.PreferredRobot(
            serviceName: "reachy_mini",
            hardwareId: "5a5e0ad96b539eae",
            lastHost: "reachy-mini.local",
            savedAt: Date(timeIntervalSince1970: 1_700_000_000)
        )
        prefs.save(robot)
        let loaded = prefs.load()
        XCTAssertEqual(loaded, robot)
    }

    func testClearRemovesSaved() {
        let (prefs, _) = makePrefs()
        prefs.save(.init(serviceName: "x", hardwareId: "y", lastHost: "z"))
        prefs.clear()
        XCTAssertNil(prefs.load())
    }
}
