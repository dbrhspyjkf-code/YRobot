import SwiftUI

/// Camera & Audio tab — spec §8.
///
/// Camera polling runs only while this view is in the active scene AND the
/// camera is enabled (spec: "only while the camera screen is visible and the
/// app is active"). Volume changes debounce at 300 ms; the slider snaps to
/// the value the robot reports back, not the user's drag position. VAD
/// updates surface persistence failures (HTTP 503 from PUT /api/audio/vad).
struct MediaView: View {
    @Environment(RobotSession.self) private var session
    @Environment(\.scenePhase) private var scenePhase

    // Camera
    @State private var cameraOn: Bool = false
    @State private var frame: Data?
    @State private var frameAt: Date?
    @State private var cameraError: String?
    @State private var cameraBusy: Bool = false

    // Volume
    @State private var volumePercent: Double = 0
    @State private var volumeMin: Double = 0
    @State private var volumeMax: Double = 100
    @State private var volumeAlsa: [Int] = [0, 60]
    @State private var volumeCommit: Task<Void, Never>?
    @State private var volumeError: String?

    // Mic upload
    @State private var micBusy: Bool = false
    @State private var micError: String?

    // VAD
    @State private var vadValue: Double = 0.1
    @State private var vadMin: Double = 0.001
    @State private var vadMax: Double = 0.5
    @State private var vadStep: Double = 0.005
    @State private var vadCommit: Task<Void, Never>?
    @State private var vadError: String?

    private let pollInterval: Duration = .milliseconds(500)
    private let debounceInterval: Duration = .milliseconds(300)

    var body: some View {
        NavigationStack {
            List {
                cameraSection
                volumeSection
                micSection
                vadSection
            }
            .navigationTitle("Camera & Audio")
            .task(id: pollKey) { await runCameraLoop() }
            .task { await loadInitialAudioState() }
            .onDisappear {
                cameraOn = false
                frame = nil
                frameAt = nil
                cameraError = nil
            }
        }
    }

    private var pollKey: String { "\(scenePhase == .active)-\(cameraOn)" }

    // MARK: - Camera section

    @ViewBuilder
    private var cameraSection: some View {
        Section("Camera") {
            Toggle("Enabled", isOn: $cameraOn)
                .onChange(of: cameraOn) { _, newValue in
                    Task { await toggleCamera(newValue) }
                }

            if let data = frame, let uiImage = UIImage(data: data) {
                Image(uiImage: uiImage)
                    .resizable()
                    .scaledToFit()
                    .frame(maxHeight: 240)
                    .accessibilityLabel("Camera preview")
                LabeledContent("Frame age",
                    value: frameAge(from: frameAt))
            } else if cameraOn {
                ProgressView()
                    .frame(maxWidth: .infinity, minHeight: 120)
            } else {
                Text("Camera off")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 120)
            }

            if let cameraError {
                Label(cameraError, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
                    .font(.footnote)
            }
        }
    }

    private func toggleCamera(_ on: Bool) async {
        cameraBusy = true
        defer { cameraBusy = false }
        do {
            let actual = try await session.setCameraEnabled(on)
            cameraOn = actual
            if !actual { frame = nil; frameAt = nil; cameraError = nil }
        } catch {
            cameraOn = !on  // revert toggle
            cameraError = "Camera toggle failed: \(error.localizedDescription)"
        }
    }

    private func runCameraLoop() async {
        // .task(id: pollKey) restarts this whenever scenePhase or cameraOn changes.
        // Only enter the polling loop when both conditions are met.
        guard scenePhase == .active, cameraOn else { return }
        while !Task.isCancelled {
            do {
                let data = try await session.currentFrame()
                frame = data
                frameAt = .now
                cameraError = nil
            } catch {
                // Per spec §8: "Retain the last valid frame briefly while
                // reporting capture failure; do not report HTTP 200 alone
                // as camera health."
                cameraError = "Frame fetch failed (\(error.localizedDescription))"
            }
            try? await Task.sleep(for: pollInterval)
        }
    }

    private func frameAge(from date: Date?) -> String {
        guard let date else { return "—" }
        let secs = Int(Date().timeIntervalSince(date))
        return secs < 1 ? "just now" : "\(secs)s ago"
    }

    // MARK: - Volume section

    @ViewBuilder
    private var volumeSection: some View {
        Section("Speaker volume") {
            Slider(value: $volumePercent, in: volumeMin...volumeMax, step: 1,
                   onEditingChanged: { editing in
                if !editing { commitVolume() }
            })
            .onChange(of: volumePercent) { _, _ in
                scheduleVolumeCommit()
            }
            LabeledContent("Current", value: "\(Int(volumePercent))%")
            LabeledContent("UI range",
                value: "\(Int(volumeMin))–\(Int(volumeMax))%")
            if volumeAlsa.count == 2 {
                LabeledContent("Hardware alsa",
                    value: "\(volumeAlsa[0])–\(volumeAlsa[1])")
            }
            if let volumeError {
                Label(volumeError, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.red).font(.footnote)
            }
        }
    }

    private func scheduleVolumeCommit() {
        volumeCommit?.cancel()
        volumeCommit = Task { [volumePercent] in
            try? await Task.sleep(for: debounceInterval)
            guard !Task.isCancelled else { return }
            do {
                let applied = try await session.setVolume(percent: Int(volumePercent))
                await MainActor.run { self.volumePercent = Double(applied) }
                volumeError = nil
            } catch {
                volumeError = "Volume update failed: \(error.localizedDescription)"
            }
        }
    }

    private func commitVolume() {
        // Flush any pending debounce and send now.
        volumeCommit?.cancel()
        Task {
            do {
                let applied = try await session.setVolume(percent: Int(volumePercent))
                await MainActor.run { self.volumePercent = Double(applied) }
                volumeError = nil
            } catch {
                volumeError = "Volume update failed: \(error.localizedDescription)"
            }
        }
    }

    // MARK: - Mic section

    @ViewBuilder
    private var micSection: some View {
        Section("Microphone") {
            if let mic = session.status?.status.audio.mic {
                let pct = max(0, min(100, Int(mic.level_percent * 100)))
                ProgressView(value: Double(pct), total: 100) {
                    Text("Level")
                }
                .accessibilityLabel("Microphone level \(pct) percent")
                LabeledContent("dB", value: String(format: "%.0f", mic.level_db))
                LabeledContent("Voice active", value: mic.voiced ? "yes" : "no")
            } else {
                Text("Mic data unavailable").foregroundStyle(.secondary)
            }

            if let inputEnabled = session.status?.status.audio.input_enabled {
                Toggle("Upload to robot", isOn: Binding(
                    get: { inputEnabled },
                    set: { newValue in Task { await setMicUpload(newValue) } }
                ))
                .disabled(micBusy)
            }
            if let micError {
                Label(micError, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.red).font(.footnote)
            }
        }
    }

    private func setMicUpload(_ enabled: Bool) async {
        micBusy = true
        defer { micBusy = false }
        do {
            _ = try await session.setMicUpload(enabled: enabled)
            micError = nil
        } catch {
            micError = "Mic upload toggle failed: \(error.localizedDescription)"
        }
    }

    // MARK: - VAD section

    @ViewBuilder
    private var vadSection: some View {
        Section("Voice-activity threshold") {
            Slider(value: $vadValue, in: vadMin...vadMax, step: vadStep,
                   onEditingChanged: { editing in
                if !editing { commitVAD() }
            })
            .onChange(of: vadValue) { _, _ in
                scheduleVADCommit()
            }
            LabeledContent("Threshold", value: String(format: "%.3f", vadValue))
            LabeledContent("Range",
                value: String(format: "%.3f–%.3f (step %.3f)", vadMin, vadMax, vadStep))
            if let vadError {
                Label(vadError, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.red).font(.footnote)
            }
        }
    }

    private func scheduleVADCommit() {
        vadCommit?.cancel()
        vadCommit = Task { [vadValue] in
            try? await Task.sleep(for: debounceInterval)
            guard !Task.isCancelled else { return }
            do {
                let applied = try await session.setVAD(rms: vadValue)
                await MainActor.run { self.vadValue = applied }
                vadError = nil
            } catch let APIError.unavailableSubsystem(msg) {
                vadError = msg ?? "Could not save VAD — persistence failed"
            } catch {
                vadError = "VAD update failed: \(error.localizedDescription)"
            }
        }
    }

    private func commitVAD() {
        vadCommit?.cancel()
        Task {
            do {
                let applied = try await session.setVAD(rms: vadValue)
                await MainActor.run { self.vadValue = applied }
                vadError = nil
            } catch let APIError.unavailableSubsystem(msg) {
                vadError = msg ?? "Could not save VAD — persistence failed"
            } catch {
                vadError = "VAD update failed: \(error.localizedDescription)"
            }
        }
    }

    // MARK: - Initial state

    private func loadInitialAudioState() async {
        // Test hook: when set via UserDefaults, automatically turn the camera
        // on so screenshot tooling can verify the preview.
        if UserDefaults.standard.bool(forKey: "autoEnableCamera") {
            await toggleCamera(true)
        }
        // Mirror the slider position to the robot's reported volume so the
        // user doesn't see a stale 0% on first appearance.
        if let s = session.status {
            await MainActor.run { self.volumePercent = Double(s.status.audio.volume_percent) }
        }
        do {
            let cfg = try await session.fetchVolumeConfig()
            await MainActor.run {
                self.volumeMin = Double(cfg.min)
                self.volumeMax = Double(cfg.max)
                self.volumeAlsa = cfg.alsa
                self.volumePercent = Double(cfg.percent)
            }
        } catch {
            // Keep the defaults if the dashboard is unavailable.
        }
        do {
            let cfg = try await session.fetchVADConfig()
            await MainActor.run {
                self.vadMin = cfg.min
                self.vadMax = cfg.max
                self.vadStep = cfg.step
                self.vadValue = cfg.rms
            }
        } catch {
            // Keep the defaults if the dashboard is unavailable.
        }
    }
}
