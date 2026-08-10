import SwiftUI

/// Overview tab — multi-card layout for the live robot state. Cards read from
/// `RobotSession`; a 5-second polling loop refreshes the status envelope while
/// a 2-second loop refreshes the chat log (spec §8 Overview).
struct OverviewView: View {
    @Environment(RobotSession.self) private var session
    @Environment(\.scenePhase) private var scenePhase

    private let statusInterval: Duration = .seconds(5)
    private let chatInterval: Duration = .seconds(2)

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("Overview")
                .task(id: scenePhase) { await statusLoop() }
                .task(id: scenePhase) { await chatLoop() }
                .refreshable { await session.refresh() }
        }
    }

    @ViewBuilder
    private var content: some View {
        if session.endpoint == nil {
            ContentUnavailableView(
                "No robot selected",
                systemImage: "antenna.radiowaves.left.and.right.slash",
                description: Text("Open the Robot tab to discover or connect.")
            )
        } else {
            List {
                if isYRobotStale {
                    StaleBanner(message: yrobotStaleMessage)
                }
                if let endpoint = session.endpoint {
                    ConnectionCard(
                        endpoint: endpoint,
                        availability: session.availability,
                        lastDaemonFetch: session.lastDaemonFetch,
                        lastYRobotFetch: session.lastYRobotFetch,
                        lastSuccessfulFetch: session.lastSuccessfulFetch
                    )
                }
                if let s = session.status {
                    RobotCard(status: s)
                    TrackingCard(status: s)
                    MotionCard(status: s)
                    AudioCard(status: s)
                    SystemCard(status: s)
                    PrivacyCard(status: s)
                }
                ConversationCard(turns: session.conversationTurns)
            }
        }
    }

    private var isYRobotStale: Bool {
        if case .unavailable = session.availability.yrobot { return true }
        return false
    }

    private var yrobotStaleMessage: String {
        let last = session.lastYRobotFetch.map {
            $0.formatted(date: .omitted, time: .standard)
        } ?? "never"
        return "YRobot dashboard unreachable — last good snapshot \(last). Other sections may be stale."
    }

    // MARK: - Polling loops

    private func statusLoop() async {
        while scenePhase == .active && !Task.isCancelled {
            await session.refresh()
            try? await Task.sleep(for: statusInterval)
        }
    }

    private func chatLoop() async {
        while scenePhase == .active && !Task.isCancelled {
            await session.probeChatLogs()
            try? await Task.sleep(for: chatInterval)
        }
    }
}
