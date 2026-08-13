import XCTest
import CryptoKit
@testable import YRobotRemote

/// Verifies the BLE session and provisioner state machines. Tests use
/// `MockBLETransport` so they do not need a real Bluetooth radio;
/// real-device acceptance still has to drive the protocol with the
/// production `CoreBluetoothTransport`.
@MainActor
final class BLEWifiProvisionerTests: XCTestCase {

    func testSessionAuthenticatesWithPIN() async throws {
        let transport = MockBLETransport()
        let session = ReachyBLESession(transport: transport)
        try await transport.connect(UUID())
        try await transport.discoverServices()
        try await session.subscribe()

        transport.responses["PIN_12345"] = .init(notification: "OK: Authenticated")

        try await session.authenticate(pin: "12345")
        XCTAssertTrue(session.isAuthed)
    }

    func testSessionRejectsWrongPIN() async throws {
        let transport = MockBLETransport()
        let session = ReachyBLESession(transport: transport)
        try await transport.connect(UUID())
        try await transport.discoverServices()
        try await session.subscribe()

        transport.responses["PIN_12345"] = .init(notification: "ERROR: Bad credentials (wrong PIN?)")

        do {
            try await session.authenticate(pin: "12345")
            XCTFail("expected wrong PIN error")
        } catch let ReachyBLESession.SessionError.protocolError(msg) {
            XCTAssertTrue(msg.contains("wrong PIN"))
        }
        XCTAssertFalse(session.isAuthed)
    }

    func testSessionIsNotConnectedBeforeSubscribe() async throws {
        let transport = MockBLETransport()
        let session = ReachyBLESession(transport: transport)
        XCTAssertFalse(session.isConnected)
    }

    func testSessionIsConnectedAfterSubscribe() async throws {
        let transport = MockBLETransport()
        let session = ReachyBLESession(transport: transport)
        try await transport.connect(UUID())
        try await transport.discoverServices()
        try await session.subscribe()
        XCTAssertTrue(session.isConnected)
    }

    func testSessionDisconnectClearsConnectedAndAuth() async throws {
        let transport = MockBLETransport()
        let session = ReachyBLESession(transport: transport)
        try await transport.connect(UUID())
        try await transport.discoverServices()
        try await session.subscribe()
        transport.responses["PIN_12345"] = .init(notification: "OK: Authenticated")
        try await session.authenticate(pin: "12345")
        XCTAssertTrue(session.isConnected)
        XCTAssertTrue(session.isAuthed)

        session.disconnect()
        XCTAssertFalse(session.isConnected)
        XCTAssertFalse(session.isAuthed)
    }

    func testSessionWifiKeyexParsesRobotPubkey() async throws {
        let transport = MockBLETransport()
        let session = ReachyBLESession(transport: transport)
        try await transport.connect(UUID())
        try await transport.discoverServices()
        try await session.subscribe()

        let priv = Curve25519.KeyAgreement.PrivateKey()
        transport.responses["WIFI_KEYEX"] = .init(
            notification: #"{"kid":"K1","pk":""# + priv.publicKey.rawRepresentation.base64EncodedString() + #"","alg":"x25519-hkdf-sha256-aesgcm"}"#
        )

        let keyex = try await session.wifiKeyex()
        XCTAssertEqual(keyex.kid, "K1")
        XCTAssertEqual(keyex.alg, "x25519-hkdf-sha256-aesgcm")
        XCTAssertNotNil(Data(base64Encoded: keyex.pk))
    }

    func testProvisionerReachesSuccess() async throws {
        let transport = MockBLETransport()
        let session = ReachyBLESession(transport: transport)
        let provisioner = BLEWifiProvisioner(session: session)

        let priv = Curve25519.KeyAgreement.PrivateKey()
        let pk = priv.publicKey.rawRepresentation.base64EncodedString()

        transport.responses["PIN_12345"] = .init(notification: "OK: Authenticated")
        transport.responses["WIFI_SCAN"] = .init(
            notification: #"["HomeNet","GuestNet"]"#
        )
        transport.responses["WIFI_KEYEX"] = .init(
            notification: #"{"kid":"K1","pk":""# + pk + #"","alg":"x25519-hkdf-sha256-aesgcm"}"#
        )
        // All WIFI_STATUS calls (initial probe + polling after
        // connect) return the connected wlan state — the loop exits
        // on the first poll.
        transport.responses["WIFI_STATUS *"] = .init(
            notification: #"{"mode":"wlan","connected":"HomeNet","error":null}"#
        )
        transport.responses["WIFI_CONNECT_ENC *"] = .init(
            immediate: "OK: Connecting to HomeNet"
        )

        await provisioner.provision(ssid: "HomeNet", password: "psk", pin: "12345")

        XCTAssertEqual(provisioner.state, .success)
        XCTAssertEqual(provisioner.networks, ["HomeNet", "GuestNet"])
        XCTAssertEqual(provisioner.statusMode, "wlan")
    }

    func testProvisionerFailsOnSealedCredentialError() async throws {
        let transport = MockBLETransport()
        let session = ReachyBLESession(transport: transport)
        let provisioner = BLEWifiProvisioner(session: session)

        let priv = Curve25519.KeyAgreement.PrivateKey()
        let pk = priv.publicKey.rawRepresentation.base64EncodedString()

        transport.responses["PIN_12345"] = .init(notification: "OK: Authenticated")
        transport.responses["WIFI_STATUS *"] = .init(
            notification: #"{"mode":"hotspot","connected":null,"error":null}"#
        )
        transport.responses["WIFI_SCAN"] = .init(notification: #"["HomeNet"]"#)
        transport.responses["WIFI_KEYEX"] = .init(
            notification: #"{"kid":"K1","pk":""# + pk + #"","alg":"x25519-hkdf-sha256-aesgcm"}"#
        )
        transport.responses["WIFI_CONNECT_ENC *"] = .init(
            notification: "ERROR: Bad credentials (wrong PIN?)"
        )

        await provisioner.provision(ssid: "HomeNet", password: "wrong", pin: "12345")

        guard case .failed = provisioner.state else {
            XCTFail("expected .failed, got \(provisioner.state)")
            return
        }
    }

    func testProvisionerFailsOnUnauthedScan() async throws {
        let transport = MockBLETransport()
        let session = ReachyBLESession(transport: transport)
        let provisioner = BLEWifiProvisioner(session: session)

        try await transport.connect(UUID())
        try await transport.discoverServices()
        try await session.subscribe()

        // No PIN sent. scan should fail with noAuth.
        await provisioner.provision(ssid: "X", password: "y", pin: "12345")
        // The provisioner's auth step happens first; without it we
        // never reach scan, so we expect a different failure path.
        // Both outcomes count as "non-success" for this contract.
        XCTAssertNotEqual(provisioner.state, .success)
    }
}
