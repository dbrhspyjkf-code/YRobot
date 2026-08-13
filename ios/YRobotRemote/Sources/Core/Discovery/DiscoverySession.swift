import Foundation
import Network

/// Bonjour browser for `_reachy-mini._tcp` services on the local network.
/// Wraps `NWBrowser` and exposes a small `@Observable` state.
///
/// Per spec §5.2: browse `_reachy-mini._tcp` on `local.` and show discovered
/// robots. Per spec §3 the service TXT records carry `hardware_id`, `version`,
/// and the robot name — we surface those so the UI can identify the robot
/// without connecting.
@Observable
@MainActor
public final class DiscoverySession {

    public struct DiscoveredRobot: Sendable, Identifiable, Hashable {
        public let serviceName: String      // Bonjour service instance name
        public let host: RobotEndpoint.Host // resolved host (.bonjourName / .ipv4)
        public let port: Int                // daemon port from TXT, default 8000
        public let hardwareId: String?
        public let version: String?
        public let robotName: String?

        public var id: String { serviceName }
    }

    public enum State: Sendable, Equatable {
        case idle
        case browsing
        case found([DiscoveredRobot])
        case failed(String)
    }

    public private(set) var state: State = .idle
    public var manualEntry: String = ""

    private var browser: NWBrowser?

    public init() {}

    /// Begin browsing. Replaces any in-flight browser. Idempotent.
    public func start() {
        stop()
        let descriptor = NWBrowser.Descriptor.bonjour(
            type: "_reachy-mini._tcp",
            domain: "local."
        )
        let parameters = NWParameters()
        parameters.includePeerToPeer = true
        let browser = NWBrowser(for: descriptor, using: parameters)
        browser.browseResultsChangedHandler = { [weak self] results, _ in
            let mapped = Self.mapResults(results)
            Task { @MainActor [weak self] in
                self?.state = .found(mapped)
            }
        }
        browser.stateUpdateHandler = { [weak self] newState in
            if case .failed(let err) = newState {
                let msg = Self.friendlyFailureMessage(for: err)
                Task { @MainActor [weak self] in
                    self?.state = .failed(msg)
                }
            }
        }
        browser.start(queue: .main)
        self.browser = browser
        state = .browsing
    }

    public func stop() {
        browser?.cancel()
        browser = nil
    }

    public func reset() {
        stop()
        state = .idle
    }

    private nonisolated static func stringValue(of entry: NWTXTRecord.Entry?) -> String? {
        switch entry {
        case .string(let s): s
        default: nil
        }
    }

    /// Public for test injection: map an NWBrowser.Result set to our domain.
    /// Pulled out so unit tests can verify TXT parsing without a real browser.
    public nonisolated static func mapResults(_ results: Set<NWBrowser.Result>) -> [DiscoveredRobot] {
        results
            .compactMap { result -> DiscoveredRobot? in
                guard case let .service(name, type, domain, _) = result.endpoint else {
                    return nil
                }
                guard type == "_reachy-mini._tcp" else { return nil }
                let record: NWTXTRecord? = {
                    if case let .bonjour(r) = result.metadata { return r }
                    return nil
                }()
                return parseBonjourRecord(serviceName: name, domain: domain, record: record)
            }
            .sorted { $0.serviceName < $1.serviceName }
    }

    /// Translate an NWBrowser failure into a message an operator can act
    /// on. The raw `String(describing: err)` for a denied Local Network
    /// permission is `DNSServiceBrowse failed: NoAuth(-65555)`, which
    /// tells the user nothing about the Settings toggle. Detect the
    /// common cases and produce a short, actionable hint.
    public nonisolated static func friendlyFailureMessage(for error: NWError) -> String {
        let raw = String(describing: error)
        let lower = raw.lowercased()
        if lower.contains("noauth") || lower.contains("-65555") {
            return "Local Network permission denied. In iOS Settings → Privacy & Security → Local Network, enable YRobot Remote, then try again."
        }
        // NWError.dns renders as "-65537: Unknown", not "dns(...)", so
        // check the enum case rather than the description text.
        if case .dns = error {
            return "Could not discover Reachy Mini over Bonjour (\(raw)). Check that the robot is on the same Wi-Fi network."
        }
        return raw
    }

    /// Pure parser used by `mapResults` and exposed for unit tests so they can
    /// exercise TXT-decoding without needing to construct `NWBrowser.Result`.
    public nonisolated static func parseBonjourRecord(
        serviceName: String,
        domain: String,
        record: NWTXTRecord?
    ) -> DiscoveredRobot? {
        var hardwareId: String?
        var version: String?
        var robotName: String?
        var daemonPort: Int = 8000
        if let record {
            hardwareId = stringValue(of: record.getEntry(for: "hardware_id"))
            version = stringValue(of: record.getEntry(for: "version"))
            robotName = stringValue(of: record.getEntry(for: "robot_name"))
            if let p = stringValue(of: record.getEntry(for: "port")),
               let parsed = Int(p) {
                daemonPort = parsed
            }
        }
        let bonjourHost = "\(serviceName).\(domain)"
        let rawHost = bonjourHost.hasSuffix(".local.")
            ? String(bonjourHost.dropLast())
            : "\(serviceName).local"
        // mDNS normalizes underscores in service names to hyphens in the
        // .local hostname so they're DNS-label legal (e.g. "reachy_mini"
        // \u2192 "reachy-mini.local"). We apply the same rewrite when
        // constructing URLs that the HTTP client will resolve.
        let host = RobotEndpoint.Host.bonjourName(rawHost.replacingOccurrences(of: "_", with: "-"))
        return DiscoveredRobot(
            serviceName: serviceName,
            host: host,
            port: daemonPort,
            hardwareId: hardwareId,
            version: version,
            robotName: robotName
        )
    }
}
