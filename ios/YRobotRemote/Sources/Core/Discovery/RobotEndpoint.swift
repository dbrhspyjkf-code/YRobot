import Foundation

/// A network endpoint that can talk to a Reachy Mini.
///
/// The robot exposes two HTTP services: the YRobot dashboard on port 8042 and
/// the official daemon on port 8000. Both share the same host, so callers only
/// carry one host + a tuple of port overrides. We default to the standard ports
/// for newly discovered or manually-typed robots and let the caller override.
public struct RobotEndpoint: Sendable, Hashable, Codable {
    public enum Host: Sendable, Hashable, Codable {
        case bonjourName(String)   // e.g. "reachy-mini.local" or "reachy_mini"
        case ipv4(String)          // e.g. "192.168.1.14"
        case hostname(String)      // e.g. "robot.lan" — resolved by system DNS

        public var displayString: String {
            switch self {
            case .bonjourName(let s): s
            case .ipv4(let s): s
            case .hostname(let s): s
            }
        }
    }

    public var host: Host
    public var daemonPort: Int   // default 8000
    public var yrobotPort: Int   // default 8042

    public init(host: Host, daemonPort: Int = 8000, yrobotPort: Int = 8042) {
        self.host = host
        self.daemonPort = daemonPort
        self.yrobotPort = yrobotPort
    }

    /// Construct URLs that the APIClient can hit. We pass these to URLComponents
    /// which percent-encodes correctly; do not call `URL(string:)` directly on
    /// user input.
    public func daemonBaseURL() throws -> URL {
        try Self.url(host: host, port: daemonPort, scheme: "http")
    }

    public func yrobotBaseURL() throws -> URL {
        try Self.url(host: host, port: yrobotPort, scheme: "http")
    }

    private static func url(host: Host, port: Int, scheme: String) throws -> URL {
        guard (1...65535).contains(port) else {
            throw RobotEndpointError.invalidPort(port)
        }
        var components = URLComponents()
        components.scheme = scheme
        components.host = host.displayString
        components.port = port
        guard let url = components.url else {
            throw RobotEndpointError.invalidHost(host.displayString)
        }
        return url
    }

    /// User-facing string. Includes both ports when they differ from defaults.
    public var displayLabel: String {
        let ports = (daemonPort == 8000 && yrobotPort == 8042)
            ? ""
            : "  (daemon :\(daemonPort), dashboard :\(yrobotPort))"
        return host.displayString + ports
    }
}

public enum RobotEndpointError: Error, Sendable, Equatable {
    case invalidPort(Int)
    case invalidHost(String)
}

/// Parses user input ("reachy-mini.local", "192.168.1.14", "robot.lan") into a
/// `RobotEndpoint.Host`. Empty / whitespace input is rejected.
public enum RobotEndpointParser {
    public static func parseHost(_ raw: String) -> RobotEndpoint.Host? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }

        // IPv4: four dotted decimal octets 0–255.
        if isIPv4(trimmed) { return .ipv4(trimmed) }
        // Bonjour-style hostnames end in `.local`.
        if trimmed.hasSuffix(".local") { return .bonjourName(trimmed) }
        // Anything else (e.g. "robot.lan", "reachy_mini") we treat as a DNS
        // hostname. `URLComponents` will accept it.
        return .hostname(trimmed)
    }

    public static func parse(_ raw: String) -> RobotEndpoint? {
        guard let host = parseHost(raw) else { return nil }
        return RobotEndpoint(host: host)
    }

    private static func isIPv4(_ s: String) -> Bool {
        let parts = s.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return false }
        for p in parts {
            guard let n = Int(p), (0...255).contains(n), String(n) == String(p) else {
                return false
            }
        }
        return true
    }
}
