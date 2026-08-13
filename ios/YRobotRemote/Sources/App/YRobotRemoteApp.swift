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
                .task {
                    // Start Bonjour browse as soon as the app is on
                    // screen. The first browse forces iOS to surface
                    // the Local Network permission prompt (or, if the
                    // user already dismissed it, list the app under
                    // Settings -> Privacy & Security -> Local Network).
                    // Without this, a user who only opens the app
                    // briefly and never navigates to the Network tab
                    // will not see YRobot Remote in the Local Network
                    // list at all, even after the permission is
                    // supposed to have been requested.
                    discovery.start()

                    // Try saved preferred first; if there is none,
                    // fall back to a manual connection to the Reachy
                    // mDNS host. This is the path that works even when
                    // iOS has not yet granted the Local Network
                    // permission (manual HTTP via NSAllowsLocalNetworking
                    // does not need Bonjour), so first-time users land
                    // on a working dashboard without having to open the
                    // Network tab and type an address.
                    if await session.restorePreferred() == nil,
                       Self.shouldAutoConnectOnLaunch {
                        _ = await session.tryManualFallback()
                    }
                }
        }
    }

    /// UserDefaults key for the auto-connect-on-launch preference.
    /// Defaults to true so a fresh install auto-connects to
    /// reachy-mini.local without any user interaction.
    private static let autoConnectKey = "YRobotRemote.autoConnectOnLaunch"
    static var shouldAutoConnectOnLaunch: Bool {
        if UserDefaults.standard.object(forKey: autoConnectKey) == nil {
            return true
        }
        return UserDefaults.standard.bool(forKey: autoConnectKey)
    }
}

/// Identifies tabs / sidebar items. Persisted to UserDefaults so the
/// app re-opens on the same screen; also lets screenshot tooling force
/// a specific tab via plutil.
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

/// Root container that adapts to the device. iPhone compact width gets
/// a `TabView`; iPad regular width gets a `NavigationSplitView` with
/// the same destinations in the sidebar plus a persistent detail
/// area for camera preview, logs, and status details.
struct RootView: View {
    @State private var selectedTab: AppTab = RootView.initialTab()
    @Environment(RobotSession.self) private var session
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass

    private static func initialTab() -> AppTab {
        let raw = UserDefaults.standard.string(forKey: "selectedTab") ?? AppTab.overview.rawValue
        return AppTab(rawValue: raw) ?? .overview
    }

    var body: some View {
        if horizontalSizeClass == .regular {
            splitRoot
        } else {
            tabRoot
        }
    }

    // MARK: - iPhone

    private var tabRoot: some View {
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

    // MARK: - iPad

    private var splitRoot: some View {
        NavigationSplitView {
            List(AppTab.allCases) { tab in
                Button {
                    selectedTab = tab
                } label: {
                    HStack {
                        Label(tab.title, systemImage: tab.icon)
                        Spacer()
                        if selectedTab == tab {
                            Image(systemName: "chevron.right")
                                .foregroundStyle(.tertiary)
                        }
                    }
                }
                .buttonStyle(.plain)
            }
            .navigationTitle("YRobot Remote")
        } detail: {
            detailView(for: selectedTab)
                .id(selectedTab)
        }
        .onChange(of: selectedTab) { _, new in
            UserDefaults.standard.set(new.rawValue, forKey: "selectedTab")
        }
    }

    @ViewBuilder
    private func detailView(for tab: AppTab) -> some View {
        switch tab {
        case .overview: OverviewView()
        case .media: MediaView()
        case .motions: MotionsView()
        case .logs: LogsView()
        case .settings: SettingsView()
        }
    }
}
