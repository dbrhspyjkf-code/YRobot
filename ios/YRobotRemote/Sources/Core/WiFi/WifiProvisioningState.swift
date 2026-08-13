import Foundation

/// Pure value type describing the Wi-Fi provisioning state machine.
/// Both the REST hotspot path and the BLE-only path consume the same
/// states; the state names are deliberately user-facing so the
/// provisioning UI can render them directly.
///
/// Transitions are owned by the coordinators (`RestWifiProvisioner`,
/// `BLEWifiProvisioner`); this file only holds the value type and
/// helpers so the views and tests can reason about each state in
/// isolation.
public enum WifiProvisioningState: Sendable, Equatable {

    /// Not connected to the robot yet; the user must pick the hotspot
    /// join flow or the BLE path first.
    case idle

    // MARK: - Setup / pre-flight
    case joiningHotspot
    case waitingForSettingsReturn
    case connectedToHotspot
    case scanningNetworks
    case selectingSSID

    // MARK: - Cryptography
    case fetchingProvisioningKey
    case sealingCredentials

    // MARK: - Connect
    case submitting
    case waitingForRobot
    case rediscovering

    // MARK: - Terminal
    case success
    case failed(reason: String)
    case cancelled

    public var isTerminal: Bool {
        switch self {
        case .success, .failed, .cancelled: true
        default: false
        }
    }

    public var userMessage: String {
        switch self {
        case .idle: "Choose how to connect: BLE or the Reachy hotspot."
        case .joiningHotspot: "Joining reachy-mini-ap…"
        case .waitingForSettingsReturn: "Open Settings to join reachy-mini-ap, then come back."
        case .connectedToHotspot: "Connected to the Reachy setup hotspot."
        case .scanningNetworks: "Scanning for nearby Wi-Fi networks…"
        case .selectingSSID: "Pick the Wi-Fi network the Reachy should join."
        case .fetchingProvisioningKey: "Fetching an encryption key from the robot…"
        case .sealingCredentials: "Encrypting the Wi-Fi password…"
        case .submitting: "Sending credentials to the robot…"
        case .waitingForRobot: "Robot is connecting. This can take a minute."
        case .rediscovering: "Robot is back online. Looking for it on your network…"
        case .success: "Reachy is on the new Wi-Fi."
        case .failed(let r): "Wi-Fi setup failed: \(r)"
        case .cancelled: "Wi-Fi setup cancelled."
        }
    }

    public var allowsCancel: Bool {
        switch self {
        case .joiningHotspot, .waitingForSettingsReturn, .connectedToHotspot,
             .scanningNetworks, .selectingSSID, .fetchingProvisioningKey,
             .sealingCredentials, .submitting, .waitingForRobot, .rediscovering:
            return true
        default:
            return false
        }
    }
}
