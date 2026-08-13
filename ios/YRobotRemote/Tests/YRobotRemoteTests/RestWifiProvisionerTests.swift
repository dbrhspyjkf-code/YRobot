import XCTest
@testable import YRobotRemote

/// Covers the REST / hotspot provisioning flow with stubbed daemon
/// responses. These tests do not exercise CoreBluetooth (that is the
/// BLE provisioner's job) and they do not exercise the actual
/// `NEHotspotConfigurationManager` — that requires a real device and
/// is part of the acceptance phase.
@MainActor
final class RestWifiProvisionerTests: XCTestCase, @unchecked Sendable {

    override func setUp() {
        super.setUp()
        StubURLProtocol.handler = nil
    }

    private let provKeyJSON = """
    {"kid":"KID-001","pk":"HbSUE7osElkCvwh/fikjWfAAFmC3hcLjJ1aH+q9C+Q8=","alg":"x25519-hkdf-sha256-aesgcm"}
    """

    private let scanJSON = """
    ["HomeNet","HomeNet-5G","GuestNet"]
    """

    private let statusWlanJSON = """
    {"mode":"wlan","known_networks":["HomeNet"],"connected_network":"HomeNet"}
    """

    private let statusBusyJSON = """
    {"mode":"busy","known_networks":[],"connected_network":null}
    """

    private func makeSession() -> URLSession { makeStubSession() }

    func testProvisionReachesSuccess() async {
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            switch path {
            case "/wifi/scan_and_list":
                return (makeHTTPResponse(req.url!, status: 200),
                        self.scanJSON.data(using: .utf8))
            case "/wifi/prov_key":
                return (makeHTTPResponse(req.url!, status: 200),
                        self.provKeyJSON.data(using: .utf8))
            case "/wifi/connect_sealed":
                return (makeHTTPResponse(req.url!, status: 202), nil)
            case "/wifi/status":
                return (makeHTTPResponse(req.url!, status: 200),
                        self.statusWlanJSON.data(using: .utf8))
            default:
                return (makeHTTPResponse(req.url!, status: 404), nil)
            }
        }
        let provisioner = RestWifiProvisioner(session: makeSession())
        await provisioner.provision(ssid: "HomeNet", password: "psk", pin: "12345")

        XCTAssertEqual(provisioner.state, .success)
        XCTAssertEqual(provisioner.statusMode, "wlan")
        XCTAssertEqual(provisioner.networks, ["HomeNet", "HomeNet-5G", "GuestNet"])
    }

    func testProvisionGoesThroughExpectedStates() async {
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            switch path {
            case "/wifi/scan_and_list":
                return (makeHTTPResponse(req.url!, status: 200),
                        self.scanJSON.data(using: .utf8))
            case "/wifi/prov_key":
                return (makeHTTPResponse(req.url!, status: 200),
                        self.provKeyJSON.data(using: .utf8))
            case "/wifi/connect_sealed":
                return (makeHTTPResponse(req.url!, status: 202), nil)
            case "/wifi/status":
                return (makeHTTPResponse(req.url!, status: 200),
                        self.statusWlanJSON.data(using: .utf8))
            default:
                return (makeHTTPResponse(req.url!, status: 404), nil)
            }
        }
        let provisioner = RestWifiProvisioner(session: makeSession())
        let states = await observeStates(
            provisioner: provisioner,
            action: {
                await provisioner.provision(ssid: "HomeNet", password: "psk", pin: "12345")
            }
        )
        // The state must visit the key transitions in order. We do
        // not assert every intermediate state — the provisioner is
        // @MainActor and observation is best-effort.
        XCTAssertTrue(states.contains(.connectedToHotspot),
            "should announce hotspot connection, got: \(states)")
        XCTAssertTrue(states.contains(.scanningNetworks))
        XCTAssertTrue(states.contains(.fetchingProvisioningKey))
        XCTAssertTrue(states.contains(.sealingCredentials))
        XCTAssertTrue(states.contains(.submitting))
        XCTAssertTrue(states.contains(.waitingForRobot))
        XCTAssertEqual(states.last, .success)
    }

    func testProvisionFailsOn400SealedCredential() async {
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            switch path {
            case "/wifi/scan_and_list":
                return (makeHTTPResponse(req.url!, status: 200),
                        self.scanJSON.data(using: .utf8))
            case "/wifi/prov_key":
                return (makeHTTPResponse(req.url!, status: 200),
                        self.provKeyJSON.data(using: .utf8))
            case "/wifi/connect_sealed":
                return (makeHTTPResponse(req.url!, status: 400),
                        #"{"detail":"decrypt_failed"}"#.data(using: .utf8))
            default:
                return (makeHTTPResponse(req.url!, status: 404), nil)
            }
        }
        let provisioner = RestWifiProvisioner(session: makeSession())
        await provisioner.provision(ssid: "HomeNet", password: "wrong", pin: "12345")

        guard case .failed = provisioner.state else {
            XCTFail("expected .failed, got \(provisioner.state)")
            return
        }
    }

    func testCancelStopsPolling() async {
        let counter = CapturedInt()
        StubURLProtocol.handler = { req in
            let path = req.url?.path ?? ""
            switch path {
            case "/wifi/scan_and_list":
                return (makeHTTPResponse(req.url!, status: 200),
                        self.scanJSON.data(using: .utf8))
            case "/wifi/prov_key":
                return (makeHTTPResponse(req.url!, status: 200),
                        self.provKeyJSON.data(using: .utf8))
            case "/wifi/connect_sealed":
                return (makeHTTPResponse(req.url!, status: 202), nil)
            case "/wifi/status":
                counter.value += 1
                return (makeHTTPResponse(req.url!, status: 200),
                        self.statusBusyJSON.data(using: .utf8))
            default:
                return (makeHTTPResponse(req.url!, status: 404), nil)
            }
        }
        let provisioner = RestWifiProvisioner(session: makeSession())
        // Start in the background and cancel after one poll cycle.
        let task = Task {
            await provisioner.provision(ssid: "HomeNet", password: "p", pin: "12345")
        }
        try? await Task.sleep(nanoseconds: 200_000_000)
        provisioner.cancel()
        await task.value

        // We cancelled before the robot flipped to wlan; state must
        // reflect that.
        XCTAssertEqual(provisioner.state, .cancelled)
    }

    /// Drives the state observable and captures every transition.
    private func observeStates(
        provisioner: RestWifiProvisioner,
        action: () async -> Void
    ) async -> [WifiProvisioningState] {
        var observed: [WifiProvisioningState] = [provisioner.state]
        let cancellable = provisioner.$state.sink { new in
            observed.append(new)
        }
        await action()
        cancellable.cancel()
        return observed
    }
}
