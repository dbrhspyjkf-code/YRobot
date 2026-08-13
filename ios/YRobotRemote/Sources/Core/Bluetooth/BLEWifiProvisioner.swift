import Foundation

/// BLE-only Wi-Fi provisioning state machine. Built on top of
/// `ReachyBLESession`; the session owns the GATT details and the
/// async-ACK command queue, the provisioner owns the user-facing
/// state and the orchestrating flow.
///
/// The flow is:
///
/// 1. subscribe to the response characteristic (must come first,
///    otherwise async commands drop their real result).
/// 2. send `PIN_<5 chars>` to open the 5-minute session.
/// 3. read `WIFI_STATUS` so the UI can show the current state.
/// 4. issue `WIFI_KEYEX` to fetch a fresh robot pubkey.
/// 5. seal the password with `WifiSealer`.
/// 6. issue `WIFI_CONNECT_ENC <json>` (async ACK) and poll status
///    until the robot reports wlan + the chosen SSID.
/// 7. disconnect BLE; the iOS app returns to the home network and
///    rediscovers the robot via Bonjour.
@MainActor
public final class BLEWifiProvisioner: ObservableObject {

    @Published public private(set) var state: WifiProvisioningState = .idle
    @Published public private(set) var networks: [String] = []
    @Published public private(set) var statusMode: String?

    private let session: ReachyBLESession
    private var task_: Task<Void, Never>?

    public init(session: ReachyBLESession) {
        self.session = session
    }

    public func reset() {
        task_?.cancel()
        task_ = nil
        state = .idle
        networks = []
        statusMode = nil
    }

    public func cancel() {
        task_?.cancel()
        if !state.isTerminal {
            state = .cancelled
        }
    }

    /// Run the full BLE provisioning flow. The caller must already
    /// have connected and discovered services (the transport does
    /// that); the provisioner owns subscribe + commands.
    public func provision(ssid: String, password: String, pin: String) async {
        let task = Task { [self] in
            await runProvision(ssid: ssid, password: password, pin: pin)
        }
        task_ = task
        await task.value
        task_ = nil
    }

    private func runProvision(ssid: String, password: String, pin: String) async {
        do {
            state = .scanningNetworks
            try await session.subscribe()

            // Auth first; most useful commands require it.
            try await session.authenticate(pin: pin)

            let status = try await session.wifiStatus()
            statusMode = status.mode

            let scan = try await session.wifiScan()
            self.networks = scan

            state = .fetchingProvisioningKey
            let keyex = try await session.wifiKeyex()

            state = .sealingCredentials
            let sealed = try WifiSealer.seal(
                ssid: ssid,
                psk: password,
                pin: pin,
                robotPublicKeyBase64: keyex.pk,
                kid: keyex.kid
            )

            state = .submitting
            try await session.wifiConnect(sealed: sealed, ssid: ssid)

            state = .rediscovering
            // The user must leave the BLE flow and rejoin the home
            // Wi-Fi; the iOS app rediscovers the robot via Bonjour
            // once the home network is back.
            state = .success
        } catch let e as ReachyBLESession.SessionError {
            if Task.isCancelled { return }
            state = .failed(reason: describe(e))
        } catch {
            if Task.isCancelled { return }
            state = .failed(reason: error.localizedDescription)
        }
    }

    private func describe(_ e: ReachyBLESession.SessionError) -> String {
        switch e {
        case .notConnected: "Not connected to the Reachy Bluetooth service"
        case .notSubscribed: "Bluetooth notifications not subscribed"
        case .noAuth: "Send a PIN first to open the BLE session"
        case .protocolError(let m): m
        case .unknownError(let m): m
        }
    }
}
