import SwiftUI

/// Settings tab — robot summary, network setup link, and the daemon + power
/// controls. Destructive actions go through a confirmation dialog per
/// spec §8 Daemon and power.
struct SettingsView: View {
    @Environment(RobotSession.self) private var session

    @State private var pending: PendingAction?
    @State private var actionError: String?
    @State private var busy: PendingAction?

    enum PendingAction: Identifiable {
        case wake, sleep, restartDaemon
        case restartYRobot, reboot, powerOff
        var id: String {
            switch self {
            case .wake: "wake"
            case .sleep: "sleep"
            case .restartDaemon: "restart-daemon"
            case .restartYRobot: "restart-yrobot"
            case .reboot: "reboot"
            case .powerOff: "power-off"
            }
        }
        var title: String {
            switch self {
            case .wake: "Wake robot?"
            case .sleep: "Sleep robot?"
            case .restartDaemon: "Restart Reachy daemon?"
            case .restartYRobot: "Restart YRobot?"
            case .reboot: "Reboot the robot?"
            case .powerOff: "Power off the robot?"
            }
        }
        var message: String {
            switch self {
            case .wake: "Robot will start motors and play the wake-up motion."
            case .sleep: "Robot will enter sleep; motors freeze and audio pauses."
            case .restartDaemon: "Daemon reconnects under the hood. Brief disconnect possible."
            case .restartYRobot: "YRobot dashboard restarts. This will disconnect the app momentarily."
            case .reboot: "Robot is put to sleep first, then rebooted. Network drops until it's back."
            case .powerOff: "Robot is put to sleep first, then powered off. You'll need physical power to bring it back."
            }
        }
        var confirmLabel: String {
            switch self {
            case .wake: "Wake"
            case .sleep: "Sleep"
            case .restartDaemon: "Restart daemon"
            case .restartYRobot: "Restart YRobot"
            case .reboot: "Reboot"
            case .powerOff: "Power off"
            }
        }
        var destructive: Bool {
            switch self {
            case .wake: false
            case .sleep: true
            case .restartDaemon: true
            case .restartYRobot: true
            case .reboot: true
            case .powerOff: true
            }
        }
    }

    var body: some View {
        NavigationStack {
            List {
                robotSection
                conversationSection
                daemonSection
                powerSection
                if let actionError {
                    Section { Text(actionError).foregroundStyle(.red).font(.footnote) }
                }
            }
            .navigationTitle("Settings")
            .confirmationDialog(
                pending?.title ?? "",
                isPresented: Binding(
                    get: { pending != nil },
                    set: { if !$0 { pending = nil } }
                ),
                titleVisibility: .visible,
                presenting: pending
            ) { action in
                Button(action.confirmLabel, role: action.destructive ? .destructive : nil) {
                    Task { await runAction(action) }
                }
                Button("Cancel", role: .cancel) {}
            } message: { action in
                Text(action.message)
            }
        }
    }

    @ViewBuilder
    private var robotSection: some View {
        Section("Robot") {
            if let endpoint = session.endpoint {
                LabeledContent("Endpoint", value: endpoint.displayLabel)
                if let hw = session.hardwareId {
                    LabeledContent("Hardware", value: String(hw.prefix(8)))
                }
                if let name = session.robotName {
                    LabeledContent("Name", value: name)
                }
            } else {
                Text("Not connected").foregroundStyle(.secondary)
            }
            NavigationLink {
                NetworkView()
            } label: {
                Label("Network setup", systemImage: "wifi")
            }
        }
    }

    @ViewBuilder
    private var conversationSection: some View {
        Section("Conversation") {
            NavigationLink {
                ConversationView()
            } label: {
                HStack {
                    Label("Backend & voice", systemImage: "bubble.left.and.bubble.right")
                    Spacer()
                    if let state = session.conversationBackend {
                        Text("\(state.running.rawValue) · \(state.connectionState.rawValue)")
                            .font(.footnote)
                            .foregroundStyle(state.connectionState == .failed ? .red : .secondary)
                    }
                }
            }
            if let state = session.conversationBackend, state.restartRequired {
                Label("Restart YRobot to apply saved changes",
                      systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
                    .font(.footnote)
            }
        }
    }

    @ViewBuilder
    private var daemonSection: some View {
        Section("Daemon") {
            Button {
                pending = .wake
            } label: {
                Label("Wake", systemImage: "sun.max")
            }
            .disabled(busy != nil)
            Button {
                pending = .sleep
            } label: {
                Label("Sleep", systemImage: "moon")
            }
            .disabled(busy != nil)
            Button(role: .destructive) {
                pending = .restartDaemon
            } label: {
                Label("Restart daemon", systemImage: "arrow.triangle.2.circlepath")
            }
            .disabled(busy != nil)
        }
    }

    @ViewBuilder
    private var powerSection: some View {
        Section("Power") {
            Button(role: .destructive) {
                pending = .restartYRobot
            } label: {
                Label("Restart YRobot", systemImage: "arrow.clockwise.circle")
            }
            .disabled(busy != nil)
            Button(role: .destructive) {
                pending = .reboot
            } label: {
                Label("Reboot robot", systemImage: "arrow.triangle.2.circlepath.circle")
            }
            .disabled(busy != nil)
            Button(role: .destructive) {
                pending = .powerOff
            } label: {
                Label("Power off", systemImage: "power")
            }
            .foregroundStyle(.red)
            .disabled(busy != nil)
            if busy != nil {
                HStack {
                    ProgressView()
                    Text("Working…")
                        .foregroundStyle(.secondary)
                        .font(.footnote)
                }
            }
        }
    }

    // MARK: - Action execution

    private func runAction(_ action: PendingAction) async {
        busy = action
        defer { busy = nil }
        actionError = nil
        do {
            switch action {
            case .wake: _ = try await session.sendDaemonAction(.wake)
            case .sleep: _ = try await session.sendDaemonAction(.sleep)
            case .restartDaemon: _ = try await session.sendDaemonAction(.restart)
            case .restartYRobot: try await session.restartYRobot()
            case .reboot: try await session.systemPower(.reboot)
            case .powerOff: try await session.systemPower(.poweroff)
            }
        } catch {
            // Spec §9: "The app does not automatically retry any destructive
            // action after a timeout because the command may already have
            // succeeded." Show the error but don't retry.
            actionError = "\(action.confirmLabel) failed: \(error.localizedDescription)"
        }
    }
}
