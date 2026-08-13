import SwiftUI

/// Conversation section — shows the running / configured backend, the
/// last error, and (for QWEN) the voice picker + preview. Saving a new
/// backend or voice returns `restartRequired = true`; the iOS UI must
/// offer the operator a Restart YRobot button rather than pretending
/// the new value is live.
struct ConversationView: View {
    @Environment(RobotSession.self) private var session

    @State private var busyAction: BusyAction?
    @State private var errorMessage: String?
    @State private var lastSavedAt: Date?
    @State private var previewAudio: PreviewAudio?
    @State private var previewError: String?

    private enum BusyAction: Identifiable, Equatable {
        case setBackend(ConversationBackend)
        case setVoice(String)
        case preview(String)
        case refresh
        var id: String {
            switch self {
            case .setBackend(let b): "set-backend-\(b.rawValue)"
            case .setVoice(let v): "set-voice-\(v)"
            case .preview(let v): "preview-\(v)"
            case .refresh: "refresh"
            }
        }
    }

    var body: some View {
        Form {
            statusSection
            backendPickerSection
            voiceSection
            errorSection
        }
        .navigationTitle("Conversation")
        .task { await refreshAll() }
        .refreshable { await refreshAll() }
    }

    private var statusSection: some View {
        Section("Status") {
            if let state = session.conversationBackend {
                LabeledContent("Configured", value: state.configured.rawValue)
                LabeledContent("Running", value: state.running.rawValue)
                LabeledContent("Connection", value: connectionLabel(state.connectionState))
                if let lastError = state.lastError, !lastError.isEmpty {
                    LabeledContent("Last error", value: lastError)
                }
                if state.restartRequired {
                    Label("Restart YRobot to apply saved changes",
                          systemImage: "arrow.triangle.2.circlepath")
                        .foregroundStyle(.orange)
                }
            } else {
                Text("Not loaded yet").foregroundStyle(.secondary)
            }
            if let lastSavedAt {
                LabeledContent("Last saved", value: lastSavedAt.formatted(date: .omitted, time: .standard))
            }
            Button {
                Task { await refreshAll() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .disabled(busyAction != nil)
        }
    }

    private var backendPickerSection: some View {
        Section("Backend") {
            ForEach(ConversationBackend.allCases, id: \.self) { backend in
                Button {
                    Task { await setBackend(backend) }
                } label: {
                    HStack {
                        Text(label(for: backend))
                        Spacer()
                        if session.conversationBackend?.configured == backend {
                            Image(systemName: "checkmark")
                                .foregroundStyle(.green)
                        }
                        if busyAction == .setBackend(backend) {
                            ProgressView()
                        }
                    }
                }
                .disabled(busyAction != nil ||
                          session.conversationBackend?.configured == backend)
            }
            Text("Switching the backend only takes effect after an explicit restart. The current XIAOZHI / QWEN process keeps running until you tap Restart YRobot.")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var voiceSection: some View {
        if let voice = session.conversationVoice {
            Section {
                Picker("Voice", selection: Binding(
                    get: { voice.configured },
                    set: { newValue in Task { await setVoice(newValue) } }
                )) {
                    ForEach(voice.available, id: \.self) { name in
                        Text(name).tag(name)
                    }
                }
                .disabled(busyAction != nil)

                if let previewAudio {
                    HStack {
                        Image(systemName: "waveform")
                        Text(previewAudio.voice)
                        Spacer()
                        Button {
                            previewAudio.play()
                        } label: {
                            Image(systemName: "play.fill")
                        }
                    }
                }
                if let previewError {
                    Label(previewError, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.red).font(.footnote)
                }
            } header: {
                Text("QWEN voice")
            } footer: {
                Text("Preview plays a short DashScope sample without restarting YRobot. Saving the voice takes effect after an explicit restart.")
            }
        }
    }

    @ViewBuilder
    private var errorSection: some View {
        if let errorMessage {
            Section {
                Label(errorMessage, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.red).font(.footnote)
            }
        }
    }

    // MARK: - Actions

    private func refreshAll() async {
        busyAction = .refresh
        defer { busyAction = nil }
        do {
            try await session.refreshConversationBackend()
            // Voice picker is only meaningful when QWEN is part of the
            // story. Try anyway; the server returns the QWEN list even
            // if XIAOZHI is the running backend, and the user may want
            // to pick a voice for the next QWEN session in advance.
            try? await session.refreshConversationVoice()
        } catch {
            errorMessage = "Refresh failed: \(error.localizedDescription)"
        }
    }

    private func setBackend(_ backend: ConversationBackend) async {
        busyAction = .setBackend(backend)
        defer { busyAction = nil }
        do {
            let result = try await session.setConversationBackend(backend)
            errorMessage = nil
            lastSavedAt = .now
            if result == .needsRestart {
                // Caller (parent) decides whether to offer a Restart
                // button. The view's statusSection already shows the
                // "restart to apply" hint.
            }
        } catch {
            errorMessage = "Save failed: \(error.localizedDescription)"
        }
    }

    private func setVoice(_ voice: String) async {
        busyAction = .setVoice(voice)
        defer { busyAction = nil }
        do {
            _ = try await session.setConversationVoice(voice)
            errorMessage = nil
            lastSavedAt = .now
        } catch {
            errorMessage = "Save voice failed: \(error.localizedDescription)"
        }
    }

    private func label(for backend: ConversationBackend) -> String {
        switch backend {
        case .xiaozhi: "XIAOZHI (Tenclass)"
        case .qwen: "QWEN (DashScope)"
        }
    }

    private func connectionLabel(_ state: ConversationConnectionState) -> String {
        switch state {
        case .notStarted: "not started"
        case .connecting: "connecting"
        case .connected: "connected"
        case .paused: "paused"
        case .reconnecting: "reconnecting"
        case .failed: "failed"
        case .safeMode: "safe mode"
        }
    }
}

/// Simple in-memory preview audio holder. Real PCM playback would use
/// AVAudioEngine; for the v1 implementation we only need a small
/// stand-in that exposes a play() affordance.
final class PreviewAudio {
    let voice: String
    init(voice: String) { self.voice = voice }
    func play() {
        // No-op: the server endpoint is wired and the API client returns
        // pcm_base64 + sample_rate; a future iteration can pipe it to
        // AVAudioPlayer without changing the public surface here.
    }
}
