import XCTest
@testable import YRobotRemote

final class KeychainStoreTests: XCTestCase {

    /// Use a unique service name per test so the tests are independent.
    private func makeStore() -> KeychainStore {
        KeychainStore(service: "ai.proma.yrobotremote.tests.\(UUID().uuidString)")
    }

    func testSaveLoadDeleteRoundTrip() throws {
        let store = makeStore()
        let payload = Data("hello-pin-12345".utf8)
        XCTAssertNil(try store.load(account: "pin"))

        try store.save(payload, for: "pin")
        let loaded = try store.load(account: "pin")
        XCTAssertEqual(loaded, payload)

        try store.delete(account: "pin")
        XCTAssertNil(try store.load(account: "pin"))
    }

    func testSaveOverwritesExisting() throws {
        let store = makeStore()
        try store.save(Data("first".utf8), for: "pin")
        try store.save(Data("second".utf8), for: "pin")
        XCTAssertEqual(try store.load(account: "pin"), Data("second".utf8))
    }

    func testDeleteMissingItemIsNotAnError() throws {
        let store = makeStore()
        XCTAssertNoThrow(try store.delete(account: "never-saved"))
    }
}
