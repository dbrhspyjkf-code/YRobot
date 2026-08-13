import SwiftUI
import UIKit

/// Discovery + manual-connection screen. Reachable from Settings and auto-
/// pushed when no robot is connected (per spec §7 Navigation).
struct NetworkView: View {
    @Environment(DiscoverySession.self) private var discovery
    @Environment(RobotSession.self) private var session
    @Environment(\.scenePhase) private var scenePhase

    @State private var manualEntry: String = NetworkView.defaultManualHost
    @State private var manualError: String?
    @State private var connectingEndpoint: RobotEndpoint?

    /// Fallback default if the user has not entered any host yet.
    /// "reachy-mini.local" is the Reachy Mini's mDNS name on Avahi
    /// and is what the official desktop app shows in its URL bar.
    static let defaultManualHost = "reachy-mini.local"
    private static let lastManualHostKey = "YRobotRemote.lastManualHost"
    private static let autoConnectKey = "YRobotRemote.autoConnectOnLaunch"

    private static var autoConnectOnLaunch: Bool {
        if UserDefaults.standard.object(forKey: autoConnectKey) == nil {
            return true
        }
        return UserDefaults.standard.bool(forKey: autoConnectKey)
    }

    var body: some View {
        Form {
            // Manual IP / hostname entry is the most reliable path and
            // works even when Bonjour is blocked (e.g. iOS Local
            // Network permission not yet granted). Show it first.
            Section {
                TextField("reachy-mini.local or 192.168.1.14", text: $manualEntry)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                if let manualError {
                    Text(manualError)
                        .font(.footnote)
                        .foregroundStyle(.red)
                }
                Button("Connect") { connectManual() }
                    .disabled(manualEntry.trimmingCharacters(in: .whitespaces).isEmpty)
            } header: {
                Text("Manual connection")
            } footer: {
                Text("Manual entry talks to the robot over plain HTTP and does not need Bonjour. Recommended when iOS has not yet granted the Local Network permission.")
            }

            Section {
                Toggle("Auto-connect on launch", isOn: Binding(
                    get: { Self.autoConnectOnLaunch },
                    set: { UserDefaults.standard.set($0, forKey: Self.autoConnectKey) }
                ))
            } header: {
                Text("Launch behaviour")
            } footer: {
                Text("When on, the app tries to connect to reachy-mini.local automatically on launch. Disable this if you manage multiple robots or use the app on a Wi-Fi without the Reachy Mini.")
            }

            Section("Discovered (Bonjour)") {
                switch discovery.state {
                case .idle:
                    Button("Start scanning") { discovery.start() }
                case .browsing:
                    HStack {
                        ProgressView()
                        Text("Scanning local network…")
                    }
                case .found(let robots):
                    if robots.isEmpty {
                        Text("No Reachy Mini found yet")
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(robots) { robot in
                            robotRow(robot)
                        }
                    }
                case .failed(let msg):
                    Label(msg, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.red)
                    if msg.contains("Local Network permission") {
                        Text("Manual entry above still works. Bonjour will work once Local Network permission is granted in iOS Settings.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                        Text("After enabling, fully quit YRobot Remote (swipe away in the app switcher) and reopen — iOS caches the denial in the running process.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    Button {
                        if let url = URL(string: UIApplication.openSettingsURLString) {
                            UIApplication.shared.open(url)
                        }
                    } label: {
                        Label("Open iOS Settings", systemImage: "gearshape")
                    }
                    Button("Retry scanning") { discovery.start() }
                    Section("Diagnostic info") {
                        LabeledContent("Bundle ID", value: Self.bundleID)
                        LabeledContent("App name", value: Self.appName)
                        LabeledContent("Has Local Network reason", value: Self.hasLocalNetworkReason ? "yes" : "NO")
                    }
                }
            }
        }
        .navigationTitle("Robot")
        .onAppear {
            // Prefill with the last host the operator successfully
            // used (or the Reachy-mDNS default for first run).
            if let last = UserDefaults.standard.string(forKey: Self.lastManualHostKey),
               !last.isEmpty {
                manualEntry = last
            } else if manualEntry.isEmpty {
                manualEntry = Self.defaultManualHost
            }
            discovery.start()
        }
        .onDisappear { discovery.stop() }
        .onChange(of: scenePhase) { _, newPhase in
            // When the user comes back from iOS Settings after
            // granting Local Network permission, restart the browse.
            // A fresh NWBrowser picks up the new permission state;
            // the previous instance may have cached the denial.
            if newPhase == .active {
                switch discovery.state {
                case .failed:
                    discovery.start()
                default:
                    break
                }
            }
        }
    }

    @ViewBuilder
    private func robotRow(_ robot: DiscoverySession.DiscoveredRobot) -> some View {
        Button {
            connect(robot: robot)
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text(robot.robotName ?? robot.serviceName)
                        .font(.headline)
                    if connectingEndpoint?.host == robot.host {
                        Spacer()
                        ProgressView()
                    }
                }
                Text(robot.host.displayString)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let v = robot.version, let hw = robot.hardwareId {
                    Text("daemon \(v) · \(hw)")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
        }
        .buttonStyle(.plain)
    }

    private func connect(robot: DiscoverySession.DiscoveredRobot) {
        let endpoint = RobotEndpoint(host: robot.host, daemonPort: robot.port)
        Task { await tryConnect(endpoint) }
    }

    private func connectManual() {
        let trimmed = manualEntry.trimmingCharacters(in: .whitespaces)
        guard let endpoint = RobotEndpointParser.parse(trimmed) else {
            manualError = "Invalid host or IP"
            return
        }
        manualError = nil
        // Remember the host the operator typed so next launch is
        // pre-filled. Storing in plain UserDefaults is fine — the
        // host is not a secret and iCloud sync is undesirable here.
        UserDefaults.standard.set(trimmed, forKey: Self.lastManualHostKey)
        Task { await tryConnect(endpoint) }
    }

    // MARK: - Diagnostic info

    private static let bundleID: String = Bundle.main.bundleIdentifier ?? "unknown"
    private static let appName: String = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String
        ?? Bundle.main.object(forInfoDictionaryKey: "CFBundleName") as? String
        ?? "unknown"
    private static let hasLocalNetworkReason: Bool = {
        guard let reason = Bundle.main.object(forInfoDictionaryKey: "NSLocalNetworkUsageDescription") as? String else {
            return false
        }
        return !reason.isEmpty
    }()

    private func tryConnect(_ endpoint: RobotEndpoint) async {
        connectingEndpoint = endpoint
        defer { connectingEndpoint = nil }
        await session.connect(to: endpoint)
    }
}
