import SwiftUI

@main
struct YRobotRemoteApp: App {
    @State private var session = RobotSession()
    @State private var discovery = DiscoverySession()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(session)
                .environment(discovery)
                .task { await session.restorePreferred() }
        }
    }
}

/// Identifies tabs. Persisted to UserDefaults so the app re-opens on the
/// same tab; also lets screenshot tooling force a specific tab via plutil.
enum AppTab: String, CaseIterable, Identifiable {
    case overview, media, motions, logs, settings
    var id: String { rawValue }
    var title: String {
        switch self {
        case .overview: "Overview"
        case .media: "Camera & Audio"
        case .motions: "Motions"
        case .logs: "Logs"
        case .settings: "Settings"
        }
    }
    var icon: String {
        switch self {
        case .overview: "rectangle.stack"
        case .media: "camera"
        case .motions: "figure.walk"
        case .logs: "text.alignleft"
        case .settings: "gearshape"
        }
    }
}

struct RootView: View {
    @State private var selectedTab: AppTab = RootView.initialTab()
    @Environment(RobotSession.self) private var session

    /// Reads the initial tab from UserDefaults on first appearance. Subsequent
    /// tab switches are held in @State and persisted via .onChange so that
    /// the SwiftUI AppStorage write-back race doesn't clobber our value
    /// during initial render.
    private static func initialTab() -> AppTab {
        let raw = UserDefaults.standard.string(forKey: "selectedTab") ?? AppTab.overview.rawValue
        return AppTab(rawValue: raw) ?? .overview
    }

    var body: some View {
        // iPhone gets a tab shell. iPad split-view lands in Stage 8 per the
        // user's decision; the TabView auto-collapses on iPhone-size
        // presentations of an iPad, which is acceptable for now.
        TabView(selection: $selectedTab) {
            OverviewView()
                .tabItem { Label(AppTab.overview.title, systemImage: AppTab.overview.icon) }
                .tag(AppTab.overview)

            MediaView()
                .tabItem { Label(AppTab.media.title, systemImage: AppTab.media.icon) }
                .tag(AppTab.media)

            MotionsView()
                .tabItem { Label(AppTab.motions.title, systemImage: AppTab.motions.icon) }
                .tag(AppTab.motions)

            LogsView()
                .tabItem { Label(AppTab.logs.title, systemImage: AppTab.logs.icon) }
                .tag(AppTab.logs)

            SettingsView()
                .tabItem { Label(AppTab.settings.title, systemImage: AppTab.settings.icon) }
                .tag(AppTab.settings)
        }
        .onChange(of: selectedTab) { _, new in
            UserDefaults.standard.set(new.rawValue, forKey: "selectedTab")
        }
    }
}
