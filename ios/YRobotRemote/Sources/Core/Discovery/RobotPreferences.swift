import Foundation

/// Persists the user's preferred robot across app launches.
///
/// We store the Bonjour service name and the daemon hardware ID as the
/// durable identity (per spec §5.5 — "Do not treat the DHCP address as the
/// permanent identity"). The last-resolved IP and the chosen service name
/// let us reconnect fast without waiting for Bonjour when the device is
/// just waking up.
///
/// Storage is `UserDefaults`: the values are not secrets, and iCloud sync is
/// not desired. The 5-char device PIN lives in Keychain (see KeychainStore).
public struct RobotPreferences: @unchecked Sendable {

    public struct PreferredRobot: Codable, Sendable, Equatable {
        public var serviceName: String      // e.g. "reachy_mini"
        public var hardwareId: String       // e.g. "5a5e0ad96b539eae"
        public var lastHost: String         // e.g. "reachy-mini.local" or "192.168.1.14"
        public var savedAt: Date

        public init(serviceName: String, hardwareId: String, lastHost: String, savedAt: Date = .now) {
            self.serviceName = serviceName
            self.hardwareId = hardwareId
            self.lastHost = lastHost
            self.savedAt = savedAt
        }
    }

    private let defaults: UserDefaults
    private let key = "preferredRobot"

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    public func load() -> PreferredRobot? {
        guard let data = defaults.data(forKey: key) else { return nil }
        return try? jsonDecoder.decode(PreferredRobot.self, from: data)
    }

    public func save(_ robot: PreferredRobot) {
        guard let data = try? jsonEncoder.encode(robot) else { return }
        defaults.set(data, forKey: key)
    }

    public func clear() {
        defaults.removeObject(forKey: key)
    }

    private let jsonEncoder: JSONEncoder = {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .iso8601
        return e
    }()

    private let jsonDecoder: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }()
}
