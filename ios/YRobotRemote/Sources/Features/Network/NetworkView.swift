import SwiftUI

/// Discovery + manual-connection screen. Reachable from Settings and auto-
/// pushed when no robot is connected (per spec §7 Navigation).
struct NetworkView: View {
    @Environment(DiscoverySession.self) private var discovery
    @Environment(RobotSession.self) private var session

    @State private var manualEntry: String = ""
    @State private var manualError: String?
    @State private var connectingEndpoint: RobotEndpoint?

    var body: some View {
        Form {
            Section("Discovered") {
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
                    Button("Retry") { discovery.start() }
                }
            }

            Section("Manual") {
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
            }
        }
        .navigationTitle("Robot")
        .onAppear { discovery.start() }
        .onDisappear { discovery.stop() }
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
        guard let endpoint = RobotEndpointParser.parse(manualEntry) else {
            manualError = "Invalid host or IP"
            return
        }
        manualError = nil
        Task { await tryConnect(endpoint) }
    }

    private func tryConnect(_ endpoint: RobotEndpoint) async {
        connectingEndpoint = endpoint
        defer { connectingEndpoint = nil }
        await session.connect(to: endpoint)
    }
}
