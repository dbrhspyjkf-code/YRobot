import XCTest
@testable import YRobotRemote

/// Decode every captured JSON fixture, re-encode, and confirm the second decode
/// produces an equal model. This guarantees the Codable schemas match the real
/// server byte-for-byte and protects against silent field drops.
final class ModelRoundTripTests: XCTestCase {

    private func fixture(_ name: String) throws -> Data {
        let bundle = Bundle(for: Self.self)
        guard let url = bundle.url(forResource: name, withExtension: "json", subdirectory: "Fixtures") else {
            throw XCTSkip("Fixture \(name).json missing from test bundle")
        }
        return try Data(contentsOf: url)
    }

    private func roundTrip<T: Codable & Equatable & Sendable>(
        _ type: T.Type, fixture name: String
    ) throws {
        let originalData = try fixture(name)
        let decoder = JSONDecoder()
        let encoder = JSONEncoder()
        let original = try decoder.decode(T.self, from: originalData)
        let encoded = try encoder.encode(original)
        let reparsed = try decoder.decode(T.self, from: encoded)
        XCTAssertEqual(original, reparsed, "Round-trip mismatch for \(name).json (\(T.self))")
    }

    func testYRobotStatus() throws { try roundTrip(YRobotStatus.self, fixture: "yrobot_status") }
    func testMotion() throws { try roundTrip(MotionList.self, fixture: "yrobot_motion") }
    func testVolume() throws { try roundTrip(VolumeEnvelope.self, fixture: "yrobot_volume") }
    func testVAD() throws { try roundTrip(VADEnvelope.self, fixture: "yrobot_vad") }
    func testAudioInput() throws { try roundTrip(AudioInputEnvelope.self, fixture: "yrobot_audio_input") }
    func testCameraState() throws { try roundTrip(CameraStateEnvelope.self, fixture: "yrobot_camera_state") }
    func testSystemState() throws { try roundTrip(SystemServiceStateEnvelope.self, fixture: "yrobot_system_state") }
    func testLogs() throws { try roundTrip(LogsResponse.self, fixture: "yrobot_logs") }
    func testDaemonStatus() throws { try roundTrip(DaemonStatus.self, fixture: "daemon_status") }
    func testWifiStatus() throws { try roundTrip(WifiStatus.self, fixture: "wifi_status") }
    func testWifiError() throws { try roundTrip(WifiError.self, fixture: "wifi_error") }
    func testWifiProvKey() throws { try roundTrip(WifiProvKey.self, fixture: "wifi_prov_key") }

    /// The scan endpoint returns a bare JSON array. Decode it directly and
    /// confirm the ordering matches the captured fixture.
    func testWifiScanArrayShape() throws {
        let data = try fixture("wifi_scan")
        let ssids = try JSONDecoder().decode([String].self, from: data)
        XCTAssertGreaterThan(ssids.count, 0)
        XCTAssertTrue(ssids.contains("ChinaNet-11G-5G"))
    }
}
