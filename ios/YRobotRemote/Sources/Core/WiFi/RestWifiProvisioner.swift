import Foundation

/// REST / hotspot path of the Wi-Fi provisioning state machine.
///
/// The user joins `reachy-mini-ap` (or accepts the iOS Hotspot
/// Configuration prompt); the daemon is then reachable at
/// `http://10.42.0.1:8000`. From there:
///
/// 1. `GET /wifi/scan_and_list` — return SSIDs.
/// 2. `GET /wifi/prov_key` — fresh robot X25519 pubkey + kid.
/// 3. `WifiSealer.seal(...)` — sealed PSK blob.
/// 4. `POST /wifi/connect_sealed` — submit.
/// 5. Poll `/wifi/status` until `mode == wlan` (or timeout).
///
/// The provisioner is intentionally a thin orchestrator: it owns
/// state transitions and timeout, but it delegates the actual HTTP
/// calls to the existing `APIClient`. Two APIClients (the regular
/// one for the current connected robot, and a hotspot-scoped one)
/// are kept side-by-side without overlap.
@MainActor
public final class RestWifiProvisioner: ObservableObject {

    @Published public private(set) var state: WifiProvisioningState = .idle
    @Published public private(set) var networks: [String] = []
    @Published public private(set) var statusMode: String?

    private let session: URLSession
    private var hotspotClient: APIClient?
    private var pollTask: Task<Void, Never>?

    public init(session: URLSession = .shared) {
        self.session = session
    }

    public func reset() {
        pollTask?.cancel()
        pollTask = nil
        state = .idle
        networks = []
        statusMode = nil
        hotspotClient = nil
    }

    public func cancel() {
        // Cooperative cancellation: the next poll iteration observes
        // `Task.isCancelled` and exits. We don't pre-empt the in-flight
        // HTTP call, but a single ~1.5s poll window is the upper bound
        // on the cancel latency.
        pollTask?.cancel()
        if !state.isTerminal {
            state = .cancelled
        }
    }

    /// Run the full provisioning flow against the setup hotspot. The
    /// caller must already have joined `reachy-mini-ap` (or accepted
    /// the HotspotConfigurationManager prompt) before calling this.
    public func provision(
        ssid: String,
        password: String,
        pin: String,
        hotspotBaseURL: URL = URL(string: "http://10.42.0.1:8000")!
    ) async {
        // Spawn a child task so cancel() can interrupt the polling
        // loop even if the caller is just `await`-ing this method.
        let task = Task { [self] in
            await self.runProvision(
                ssid: ssid,
                password: password,
                pin: pin,
                hotspotBaseURL: hotspotBaseURL
            )
        }
        pollTask = task
        await task.value
        pollTask = nil
    }

    private func runProvision(
        ssid: String,
        password: String,
        pin: String,
        hotspotBaseURL: URL
    ) async {
        guard !state.isTerminal else { return }
        if case .idle = state {
            state = .connectedToHotspot
        }

        do {
            // 1. scan
            state = .scanningNetworks
            let client = APIClient(baseURL: hotspotBaseURL, session: session)
            self.hotspotClient = client
            let scan: [String] = try await client.get("/wifi/scan_and_list", query: [])
            self.networks = scan

            // 2. provisioning key
            state = .fetchingProvisioningKey
            let key: WifiProvKey = try await client.get("/wifi/prov_key")

            // 3. seal
            state = .sealingCredentials
            let sealed = try WifiSealer.seal(
                ssid: ssid,
                psk: password,
                pin: pin,
                robotPublicKeyBase64: key.pk,
                kid: key.kid
            )

            // 4. submit
            state = .submitting
            try await client.postDiscarding(
                "/wifi/connect_sealed",
                body: sealed,
                query: []
            )

            // 5. poll status
            state = .waitingForRobot
            try await pollStatus(client: client, timeout: 90)

            state = .rediscovering
            // The hotspot is going down momentarily. Caller decides
            // when to rediscover on the destination network.
            state = .success
        } catch {
            // Cancellation is intentional and the caller will have
            // already set state to .cancelled — don't overwrite.
            if Task.isCancelled { return }
            state = .failed(reason: error.localizedDescription)
        }
    }

    /// Poll /wifi/status until the mode flips to "wlan" or the
    /// timeout elapses. The daemon's 202 Accepted response means the
    /// connect_sealed is queued; we keep polling until nmcli reports
    /// success. Cooperative cancellation: the loop checks
    /// `Task.isCancelled` on every iteration so `cancel()` returns
    /// control within one poll interval.
    private func pollStatus(client: APIClient, timeout: TimeInterval) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if Task.isCancelled { throw RestWifiError.timeout }
            let status: WifiStatus = try await client.get("/wifi/status")
            statusMode = status.mode.rawValue
            if status.mode == .wlan {
                return
            }
            try await Task.sleep(nanoseconds: 1_500_000_000)
        }
        throw RestWifiError.timeout
    }
}

public enum RestWifiError: Error, Sendable, Equatable {
    case timeout
    case invalidRobotKey(String)
}
