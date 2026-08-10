import SwiftUI

/// Logs tab — server-side filter + min_level (spec §8 Logs).
///
/// Filter UI maps:
///   "Chat"     \u2192 filter=chat   (categorical: keep only `xz stt:` / `xz tts text:`)
///   "Debug"    \u2192 min_level=debug   (most verbose)
///   "Info"     \u2192 min_level=info
///   "Notice"   \u2192 min_level=notice
///   "Warning"  \u2192 min_level=warning
///   "Error"    \u2192 min_level=error   (least verbose)
///
/// Ring buffer holds up to 2,000 rows in memory. "Follow" is the default;
/// scrolling up auto-pauses, scrolling back to bottom re-engages. Clear
/// removes only what the app holds \u2014 it does not touch the robot journal.
struct LogsView: View {
    @Environment(RobotSession.self) private var session
    @Environment(\.scenePhase) private var scenePhase

    @State private var entries: [LogEntry] = []
    @State private var chatFilterEnabled: Bool = false
    @State private var minLevel: LogMinLevel = .info
    @State private var isFollowing: Bool = true
    @State private var fetchError: String?

    private let pollInterval: Duration = .seconds(2)
    private let maxRows: Int = 2000

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                filterBar
                Divider()
                logList
            }
            .navigationTitle("Logs")
            .task(id: "\(scenePhase == .active)-\(minLevel)-\(chatFilterEnabled)") { await runPoll() }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    HStack(spacing: 12) {
                        Button {
                            entries.removeAll(keepingCapacity: true)
                        } label: {
                            Image(systemName: "trash")
                        }
                        .accessibilityLabel("Clear local log buffer")
                        Button {
                            isFollowing = true
                        } label: {
                            Image(systemName: isFollowing ? "pause.circle.fill" : "play.circle")
                        }
                        .accessibilityLabel(isFollowing ? "Pause follow" : "Resume follow")
                    }
                }
            }
        }
    }

    private var filterBar: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                FilterChip(
                    label: "Chat",
                    active: chatFilterEnabled,
                    color: .orange
                ) {
                    chatFilterEnabled.toggle()
                    if chatFilterEnabled { minLevel = .info }
                }
                Divider().frame(height: 18)
                ForEach(LogMinLevel.allCases.reversed(), id: \.self) { level in
                    FilterChip(
                        label: label(for: level),
                        active: !chatFilterEnabled && minLevel == level,
                        color: color(for: level)
                    ) {
                        chatFilterEnabled = false
                        minLevel = level
                    }
                }
            }
            .padding(.horizontal)
            .padding(.vertical, 8)
        }
    }

    private func label(for level: LogMinLevel) -> String {
        switch level {
        case .debug: "Debug"
        case .info: "Info"
        case .notice: "Notice"
        case .warning: "Warning"
        case .error: "Error"
        }
    }

    private func color(for level: LogMinLevel) -> Color {
        switch level {
        case .debug: .gray
        case .info: .blue
        case .notice: .purple
        case .warning: .orange
        case .error: .red
        }
    }

    @ViewBuilder
    private var logList: some View {
        if let fetchError, entries.isEmpty {
            ContentUnavailableView(
                "Failed to load",
                systemImage: "exclamationmark.triangle",
                description: Text(fetchError)
            )
        } else {
            ScrollViewReader { proxy in
                List {
                    ForEach(Array(entries.enumerated()), id: \.offset) { idx, entry in
                        LogRow(entry: entry, color: colorForLevel(entry.level))
                            .id(idx)
                    }
                    if isFollowing {
                        Color.clear.frame(height: 1).id("bottom")
                    }
                }
                .listStyle(.plain)
                .font(.system(.caption, design: .monospaced))
                .onChange(of: entries.count) { _, _ in
                    if isFollowing {
                        withAnimation(.none) {
                            proxy.scrollTo("bottom", anchor: .bottom)
                        }
                    }
                }
                .simultaneousGesture(
                    DragGesture().onChanged { value in
                        // Heuristic: dragging down at the top, or any upward
                        // drag, disengages follow. Re-engaging requires the
                        // explicit toolbar button (spec: "Pause following
                        // when the user scrolls away from the bottom").
                        if value.translation.height < -20 { isFollowing = false }
                    }
                )
            }
        }
    }

    private func colorForLevel(_ level: String) -> Color {
        switch level.lowercased() {
        case "emerg", "alert", "crit", "error": .red
        case "warning": .orange
        case "notice": .purple
        case "info": .blue
        default: .secondary
        }
    }

    // MARK: - Polling

    private func runPoll() async {
        // Restart whenever scenePhase / filters change.
        guard scenePhase == .active else { return }
        // Clear stale buffer when filters change so we don't show mixed
        // categories from previous polls.
        entries.removeAll(keepingCapacity: true)
        fetchError = nil
        while !Task.isCancelled && scenePhase == .active {
            do {
                let resp = try await session.fetchLogs(
                    minLevel: minLevel,
                    kind: chatFilterEnabled ? .chat : nil,
                    lines: maxRows
                )
                mergeNew(resp.logs)
                fetchError = nil
            } catch {
                fetchError = error.localizedDescription
                // Keep previous entries visible (spec §9 "Connection loss
                // shows the last successful timestamp").
            }
            try? await Task.sleep(for: pollInterval)
        }
    }

    private func mergeNew(_ incoming: [LogEntry]) {
        // Server returns entries sorted by timestamp_us ascending. We keep
        // a deduplicated ring of up to maxRows.
        var seen = Set<Int64>()
        for e in entries { seen.insert(e.timestamp_us) }
        var combined = entries
        for e in incoming where !seen.contains(e.timestamp_us) {
            combined.append(e)
            seen.insert(e.timestamp_us)
        }
        if combined.count > maxRows {
            combined.removeFirst(combined.count - maxRows)
        }
        entries = combined
    }
}

private struct LogRow: View {
    let entry: LogEntry
    let color: Color
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(entry.timestamp)
                .foregroundStyle(.secondary)
            Text("[\(entry.level)]")
                .foregroundStyle(color)
            Text(entry.message)
                .lineLimit(4)
        }
    }
}

private struct FilterChip: View {
    let label: String
    let active: Bool
    let color: Color
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(label)
                .font(.callout.bold())
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .background(active ? color : color.opacity(0.18))
                .foregroundStyle(active ? .white : color)
                .clipShape(Capsule())
        }
    }
}
