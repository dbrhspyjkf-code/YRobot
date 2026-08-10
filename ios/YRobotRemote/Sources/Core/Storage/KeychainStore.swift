import Foundation
import Security

/// Minimal Keychain wrapper for the 5-char device PIN and any saved connection
/// metadata. Single item per service+account. Used by Wi-Fi provisioning and
/// connection persistence; spec §5 / §6.
public struct KeychainStore: Sendable {
    public let service: String

    public init(service: String = "ai.proma.yrobotremote") {
        self.service = service
    }

    public func save(_ data: Data, for account: String) throws {
        // Delete first — Keychain updates via SecItemUpdate require the
        // exact same attributes that SecItemAdd needs, which is fiddlier
        // than just removing + re-adding.
        try? delete(account: account)

        let attrs: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]
        let status = SecItemAdd(attrs as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw KeychainError.osStatus(status)
        }
    }

    public func load(account: String) throws -> Data? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        switch status {
        case errSecSuccess:
            return item as? Data
        case errSecItemNotFound:
            return nil
        default:
            throw KeychainError.osStatus(status)
        }
    }

    public func delete(account: String) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError.osStatus(status)
        }
    }
}

public enum KeychainError: Error, Sendable, Equatable {
    case osStatus(OSStatus)
}
