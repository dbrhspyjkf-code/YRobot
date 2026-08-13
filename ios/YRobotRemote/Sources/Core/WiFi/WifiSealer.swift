import Foundation
import CryptoKit

/// Implements the official Reachy Mini sealed-WiFi-password protocol
/// (`x25519-hkdf-sha256-aesgcm`) on top of Apple's CryptoKit. The same
/// derivation runs on the daemon, so the iOS code must match byte-for-byte
/// (info string, salt, AAD, nonce length, tag length).
///
/// Spec reference: `.context/protocol/BLE_WIFI_PROVISIONING.md`.
public enum WifiSealer {

    public static let info = "reachy-mini-wifi-psk-v1"
    public static let keyByteCount = 32
    public static let nonceByteCount = 12
    public static let tagByteCount = 16

    public struct SealedPayload: Codable, Sendable, Equatable {
        public let ssid: String
        public let kid: String
        public let epk: String          // base64 of phone ephemeral pubkey (32B)
        public let nonce: String        // base64 of 12B nonce
        public let ct: String           // base64 of ciphertext || 16B tag
    }

    /// Errors raised by the sealing path. All are surfaced to the user as
    /// sealedCredential API errors so the UI can prompt for a fresh
    /// key exchange or a PIN re-entry.
    public enum SealError: Error, Sendable, Equatable {
        case invalidRobotPublicKey(String)
        case sealFailed(String)
        case serializationFailed(String)
    }

    /// Seal a WiFi password for `WIFI_CONNECT_ENC` (BLE) or
    /// `/wifi/connect_sealed` (REST). Returns the JSON body the caller
    /// must hand to the daemon. The phone's ephemeral private key is
    /// thrown away after sealing — there is no need to keep it around.
    public static func seal(
        ssid: String,
        psk: String,
        pin: String,
        robotPublicKeyBase64: String,
        kid: String
    ) throws -> SealedPayload {
        let robotPub: Curve25519.KeyAgreement.PublicKey
        guard let raw = Data(base64Encoded: robotPublicKeyBase64) else {
            throw SealError.invalidRobotPublicKey("robot pk is not valid base64")
        }
        do {
            robotPub = try Curve25519.KeyAgreement.PublicKey(rawRepresentation: raw)
        } catch {
            throw SealError.invalidRobotPublicKey(String(describing: error))
        }

        let myPriv = Curve25519.KeyAgreement.PrivateKey()
        let shared: SharedSecret
        do {
            shared = try myPriv.sharedSecretFromKeyAgreement(with: robotPub)
        } catch {
            throw SealError.sealFailed("ecdh: \(error.localizedDescription)")
        }

        let key: SymmetricKey = shared.hkdfDerivedSymmetricKey(
            using: SHA256.self,
            salt: Data(pin.utf8),
            sharedInfo: Data(info.utf8),
            outputByteCount: keyByteCount
        )

        // AAD = SSID binds the sealed PSK to its target network; a
        // captured blob cannot be replayed to seal-connect a different
        // SSID.
        let sealed: AES.GCM.SealedBox
        do {
            sealed = try AES.GCM.seal(
                Data(psk.utf8),
                using: key,
                authenticating: Data(ssid.utf8)
            )
        } catch {
            throw SealError.sealFailed("aes-gcm seal: \(error.localizedDescription)")
        }

        // ct is ciphertext || 16-byte tag (Python's AESGCM.decrypt
        // expects the tag appended). CryptoKit exposes them separately
        // — concatenate.
        let ciphertextAndTag = sealed.ciphertext + sealed.tag

        let payload = SealedPayload(
            ssid: ssid,
            kid: kid,
            epk: myPriv.publicKey.rawRepresentation.base64EncodedString(),
            nonce: Data(sealed.nonce).base64EncodedString(),
            ct: ciphertextAndTag.base64EncodedString()
        )

        // Sanity: nonce must be exactly 12B, key 32B, tag 16B.
        guard Data(base64Encoded: payload.nonce)?.count == nonceByteCount else {
            throw SealError.sealFailed("nonce length != \(nonceByteCount)")
        }
        guard ciphertextAndTag.count >= tagByteCount else {
            throw SealError.sealFailed("ct+tag too short")
        }

        return payload
    }

    /// Convenience that produces the JSON body the daemon expects.
    public static func sealJSON(
        ssid: String,
        psk: String,
        pin: String,
        robotPublicKeyBase64: String,
        kid: String,
        encoder: JSONEncoder = JSONEncoder()
    ) throws -> Data {
        let payload = try seal(
            ssid: ssid,
            psk: psk,
            pin: pin,
            robotPublicKeyBase64: robotPublicKeyBase64,
            kid: kid
        )
        encoder.outputFormatting = [.withoutEscapingSlashes]
        do {
            return try encoder.encode(payload)
        } catch {
            throw SealError.serializationFailed(String(describing: error))
        }
    }
}
