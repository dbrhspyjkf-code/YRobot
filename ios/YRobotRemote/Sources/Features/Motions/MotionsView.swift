import SwiftUI

/// Motions tab — basic/emotion/dance categorization with Chinese display
/// labels (spec §8 Motions). Buttons disable while a play request is in
/// flight; no automatic retry on app-lock / daemon-busy errors.
struct MotionsView: View {
    @Environment(RobotSession.self) private var session

    @State private var moves: [String] = []
    @State private var inFlight: String?
    @State private var lastError: String?
    @State private var loadError: String?

    private let category = MotionCategory()

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("Motions")
                .task { await loadMotions() }
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button {
                            Task { await loadMotions() }
                        } label: {
                            Image(systemName: "arrow.clockwise")
                        }
                        .accessibilityLabel("Reload motion list")
                    }
                }
        }
    }

    @ViewBuilder
    private var content: some View {
        if moves.isEmpty {
            ContentUnavailableView(
                loadError ?? "No motions",
                systemImage: "figure.walk.motion",
                description: loadError == nil ? Text("Tap reload to retry") : nil
            )
        } else {
            List {
                ForEach(category.bucketed(moves), id: \.0) { kind, names in
                    Section(kind.title) {
                        ForEach(names, id: \.self) { name in
                            motionRow(name)
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func motionRow(_ name: String) -> some View {
        let playing = inFlight == name
        Button {
            Task { await play(name) }
        } label: {
            HStack {
                Text(category.displayName(for: name))
                    .foregroundStyle(.primary)
                Spacer()
                if playing {
                    ProgressView()
                } else if session.status?.status.motion.current_move == name {
                    Image(systemName: "play.fill")
                        .foregroundStyle(.green)
                }
            }
            .opacity(inFlight != nil && !playing ? 0.5 : 1.0)
        }
        .disabled(inFlight != nil)
    }

    private func loadMotions() async {
        loadError = nil
        do {
            let list = try await session.fetchMotions()
            moves = list.moves
        } catch {
            loadError = error.localizedDescription
        }
    }

    private func play(_ name: String) async {
        inFlight = name
        defer { inFlight = nil }
        do {
            let resp = try await session.playMotion(name: name)
            lastError = nil
            _ = resp
        } catch let APIError.validation(msg) {
            lastError = "Unknown motion: \(msg)"
        } catch let APIError.robotBusy(msg) {
            lastError = msg ?? "Robot is busy"
        } catch {
            lastError = "Failed: \(error.localizedDescription)"
        }
    }
}
