import Foundation
@preconcurrency import CoreBluetooth

/// BLE GATT UUIDs and command vocabulary for the Reachy Mini Wi-Fi
/// provisioning service. The constants match the official daemon's
/// `bluetooth_service.py` exactly — there is no negotiation, the
/// robot will only respond to these specific service and
/// characteristic UUIDs.
public enum ReachyBLEConstants {
    /// Primary command service advertised by the robot.
    public static let serviceUUID = CBUUID(string: "12345678-1234-5678-1234-56789abcdef0")
    /// Write-only command characteristic.
    public static let commandUUID = CBUUID(string: "12345678-1234-5678-1234-56789abcdef1")
    /// Read + notify response characteristic.
    public static let responseUUID = CBUUID(string: "12345678-1234-5678-1234-56789abcdef2")

    /// Default Reachy Mini BLE peripheral name prefix. The robot may
    /// also be discovered via its hardware-id TXT record once paired.
    public static let peripheralNamePrefix = "reachy"
}

/// GATT operations the higher-level session depends on. Production code
/// uses `CoreBluetoothTransport` (Core Bluetooth). Tests use a
/// `MockBLETransport` that drives the same protocol from a fixture
/// without ever touching CoreBluetooth.
public protocol ReachyBLETransport: AnyObject, Sendable {
    /// Begin scanning for Reachy Mini peripherals. The completion
    /// fires once per advertisement received.
    func startScan(onPeripheral: @escaping @Sendable (UUID, String) -> Void) async
    func stopScan() async

    /// Connect to a previously-seen peripheral. Throws on failure.
    func connect(_ peripheralID: UUID) async throws

    /// Discover the Reachy command service and its two characteristics.
    /// Must be called after `connect` succeeds.
    func discoverServices() async throws

    /// Subscribe to the response characteristic. After this point, all
    /// notifications received from the robot are forwarded to `onNotify`.
    /// MUST be called before any async command (`WIFI_SCAN`,
    /// `WIFI_STATUS`, `WIFI_CONNECT_ENC`, `WIFI_FORGET`) — those
    /// commands return `OK: working` immediately and deliver the real
    /// result through a later notification.
    func subscribe(onNotify: @escaping @Sendable (Data) -> Void) async throws

    /// Write a UTF-8 command to the command characteristic. Some
    /// commands (PING, PIN_*, WIFI_KEYEX) reply synchronously; others
    /// reply asynchronously via the previously-registered `onNotify`.
    func writeCommand(_ text: String) async throws

    func disconnect() async
}
