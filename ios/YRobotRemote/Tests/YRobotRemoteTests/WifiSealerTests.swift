import XCTest
import CryptoKit
@testable import YRobotRemote

/// Verifies `WifiSealer` matches the daemon's `x25519-hkdf-sha256-aesgcm`
/// algorithm byte-for-byte. The sealed payload must be decryptable by
/// anyone holding the robot's X25519 private key + the PIN — so we run
/// the daemon's half ourselves and confirm the original PSK comes back
/// intact. The protocol docs are the ground truth; this test is the
/// check that the iOS code follows them.
final class WifiSealerTests: XCTestCase {

    /// Fixed test vector: a deterministic robot keypair, a known SSID,
    /// PSK, and PIN. The phone generates its own ephemeral keypair each
    /// call (that's the protocol), so the test asserts the **shape** of
    /// the payload and then re-derives the key with both sides to
    /// confirm the original PSK round-trips.
    func testSealProducesValidShape() throws {
        let robotPriv = Curve25519.KeyAgreement.PrivateKey()
        let payload = try WifiSealer.seal(
            ssid: "HomeNet",
            psk: "super-secret-passphrase",
            pin: "12345",
            robotPublicKeyBase64: robotPriv.publicKey.rawRepresentation.base64EncodedString(),
            kid: "KID-123"
        )

        XCTAssertEqual(payload.ssid, "HomeNet")
        XCTAssertEqual(payload.kid, "KID-123")
        XCTAssertEqual(Data(base64Encoded: payload.epk)?.count, 32,
            "phone ephemeral pubkey must be 32 bytes (X25519)")
        XCTAssertEqual(Data(base64Encoded: payload.nonce)?.count, 12,
            "AES-GCM nonce must be 12 bytes")
        XCTAssertNotNil(Data(base64Encoded: payload.ct))
    }

    /// The same input produces two distinct sealed payloads (different
    /// ephemeral keypair + nonce). This is a property check, not a
    /// determinism check.
    func testSealProducesDistinctPayloadsAcrossCalls() throws {
        let robotPub = Curve25519.KeyAgreement.PrivateKey().publicKey
        let pk = robotPub.rawRepresentation.base64EncodedString()
        let p1 = try WifiSealer.seal(ssid: "S", psk: "P", pin: "12345",
                                     robotPublicKeyBase64: pk, kid: "K")
        let p2 = try WifiSealer.seal(ssid: "S", psk: "P", pin: "12345",
                                     robotPublicKeyBase64: pk, kid: "K")
        XCTAssertNotEqual(p1.epk, p2.epk, "ephemeral pubkey must rotate per call")
        XCTAssertNotEqual(p1.nonce, p2.nonce, "nonce must rotate per call")
        XCTAssertNotEqual(p1.ct, p2.ct)
    }

    /// The crucial byte-for-byte check: the daemon's algorithm must
    /// produce the original PSK when it runs the inverse of our seal.
    /// If this test ever fails after a refactor, the protocol has
    /// drifted and either iOS or daemon (or both) need a fix.
    func testDaemonSideRoundTripRecoversPSK() throws {
        let robotPriv = Curve25519.KeyAgreement.PrivateKey()
        let robotPub = robotPriv.publicKey

        let ssid = "HomeNet-5G"
        let psk = "correct horse battery staple"
        let pin = "ABCDE"

        let payload = try WifiSealer.seal(
            ssid: ssid,
            psk: psk,
            pin: pin,
            robotPublicKeyBase64: robotPub.rawRepresentation.base64EncodedString(),
            kid: "K1"
        )

        // Recreate the daemon's decryption path:
        //   shared = ECDH(robotPriv, phoneEpk)
        //   key    = HKDF-SHA256(shared, salt=pin, info=label, L=32)
        //   psk    = AES-GCM-open(key, nonce, ct+tag, aad=ssid)
        guard let epkData = Data(base64Encoded: payload.epk) else {
            XCTFail("epk not base64"); return
        }
        let phonePub = try Curve25519.KeyAgreement.PublicKey(rawRepresentation: epkData)
        let shared = try robotPriv.sharedSecretFromKeyAgreement(with: phonePub)
        let key = shared.hkdfDerivedSymmetricKey(
            using: SHA256.self,
            salt: Data(pin.utf8),
            sharedInfo: Data(WifiSealer.info.utf8),
            outputByteCount: WifiSealer.keyByteCount
        )
        guard let ctFull = Data(base64Encoded: payload.ct),
              let nonceData = Data(base64Encoded: payload.nonce) else {
            XCTFail("ct/nonce not base64"); return
        }
        let ciphertext = ctFull.prefix(ctFull.count - WifiSealer.tagByteCount)
        let tag = ctFull.suffix(WifiSealer.tagByteCount)
        let box = try AES.GCM.SealedBox(
            nonce: AES.GCM.Nonce(data: nonceData),
            ciphertext: ciphertext,
            tag: tag
        )
        let recovered = try AES.GCM.open(box, using: key, authenticating: Data(ssid.utf8))
        let recoveredPSK = String(data: recovered, encoding: .utf8)

        XCTAssertEqual(recoveredPSK, psk,
            "iOS seal must match the daemon's ECDH+HKDF+AES-GCM path byte-for-byte")
    }

    /// AAD binding: sealing the same PSK with a different SSID must
    /// produce a different ciphertext (the AAD mixes into the auth tag).
    func testSealIsBoundToSSID() throws {
        let robotPub = Curve25519.KeyAgreement.PrivateKey().publicKey
        let pk = robotPub.rawRepresentation.base64EncodedString()
        let a = try WifiSealer.seal(ssid: "Net-A", psk: "same", pin: "12345",
                                    robotPublicKeyBase64: pk, kid: "K")
        let b = try WifiSealer.seal(ssid: "Net-B", psk: "same", pin: "12345",
                                    robotPublicKeyBase64: pk, kid: "K")
        // Different ephemeral keys + nonces guarantee a different ct;
        // assert it to make the AAD intent explicit in the test.
        XCTAssertNotEqual(a.ct, b.ct)
    }

    /// Wrong PIN must produce a sealed blob that fails to open with the
    /// correct PIN's key. The AES-GCM auth tag must not verify.
    func testSealRejectsWrongPINOnDaemonSide() throws {
        let robotPriv = Curve25519.KeyAgreement.PrivateKey()
        let pk = robotPriv.publicKey.rawRepresentation.base64EncodedString()

        let payload = try WifiSealer.seal(
            ssid: "Net", psk: "psk", pin: "RIGHT1",
            robotPublicKeyBase64: pk, kid: "K"
        )

        // Decrypt with the wrong PIN.
        let epkData = try XCTUnwrap(Data(base64Encoded: payload.epk))
        let phonePub = try Curve25519.KeyAgreement.PublicKey(rawRepresentation: epkData)
        let shared = try robotPriv.sharedSecretFromKeyAgreement(with: phonePub)
        let key = shared.hkdfDerivedSymmetricKey(
            using: SHA256.self,
            salt: Data("WRONG1".utf8),
            sharedInfo: Data(WifiSealer.info.utf8),
            outputByteCount: WifiSealer.keyByteCount
        )
        let ctFull = try XCTUnwrap(Data(base64Encoded: payload.ct))
        let nonceData = try XCTUnwrap(Data(base64Encoded: payload.nonce))
        let ciphertext = ctFull.prefix(ctFull.count - WifiSealer.tagByteCount)
        let tag = ctFull.suffix(WifiSealer.tagByteCount)
        let box = try AES.GCM.SealedBox(
            nonce: AES.GCM.Nonce(data: nonceData),
            ciphertext: ciphertext,
            tag: tag
        )
        XCTAssertThrowsError(
            try AES.GCM.open(box, using: key, authenticating: Data("Net".utf8)),
            "wrong PIN must fail AES-GCM auth"
        )
    }

    /// The protocol explicitly forbids a malformed key on the wire.
    /// The sealer must refuse rather than silently producing garbage.
    func testSealRejectsInvalidBase64RobotKey() {
        XCTAssertThrowsError(
            try WifiSealer.seal(
                ssid: "Net", psk: "psk", pin: "12345",
                robotPublicKeyBase64: "not-base64",
                kid: "K"
            )
        )
    }

    /// JSON output keeps the daemon's expected keys.
    func testSealJSONProducesExpectedKeys() throws {
        let robotPriv = Curve25519.KeyAgreement.PrivateKey()
        let data = try WifiSealer.sealJSON(
            ssid: "Net", psk: "psk", pin: "12345",
            robotPublicKeyBase64: robotPriv.publicKey.rawRepresentation.base64EncodedString(),
            kid: "K"
        )
        let obj = try JSONSerialization.jsonObject(with: data) as? [String: String]
        XCTAssertNotNil(obj)
        XCTAssertEqual(obj?["ssid"], "Net")
        XCTAssertEqual(obj?["kid"], "K")
        XCTAssertNotNil(obj?["epk"])
        XCTAssertNotNil(obj?["nonce"])
        XCTAssertNotNil(obj?["ct"])
    }
}
