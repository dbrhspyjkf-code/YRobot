import Foundation

/// Typed error model for API + lower-level failures.
/// Spec §9 calls for: discovery, permission, connection, timeout, validation,
/// robot busy, unavailable subsystem, destructive-action-unknown-result.
/// This file covers the ones the transport layer can produce directly; feature
/// layers will wrap these (e.g. `.unavailableSubsystem(...)`).
public enum APIError: Error, Sendable, Equatable {
    /// Underlying transport failure (DNS, refused connection, TLS, etc.).
    /// The associated value is the raw URLError code description for logs.
    case transport(String)

    /// Request did not complete within the configured deadline.
    case timeout

    /// Non-2xx response with no parseable FastAPI `{"detail": ...}` body.
    case httpStatus(Int)

    /// Response body could not be decoded into the expected model.
    case decodingFailed(String)

    /// HTTP 422 — caller-supplied data was rejected by the server.
    case validation(String)

    /// HTTP 409 — the daemon or robot is already busy with another op.
    case robotBusy(String?)

    /// HTTP 503 — a known subsystem (volume, logs, audio, etc.) is unavailable.
    /// The associated value is the server detail if any.
    case unavailableSubsystem(String?)

    /// HTTP 400 from a sealed-connect path (wrong PIN / tampered blob / stale kid).
    /// Kept distinct from generic validation because the UI wants to show a
    /// specific message and offer a one-shot re-keyex retry (spec §9).
    case sealedCredential(String)

    public var localizedDescription: String {
        switch self {
        case .transport(let m): "Network: \(m)"
        case .timeout: "Request timed out"
        case .httpStatus(let c): "HTTP \(c)"
        case .decodingFailed(let m): m.isEmpty ? "Could not decode server response" : "Could not decode response: \(m)"
        case .validation(let m): m.isEmpty ? "Rejected by server" : m
        case .robotBusy(let m): m ?? "Robot is busy"
        case .unavailableSubsystem(let m): m ?? "Subsystem unavailable"
        case .sealedCredential(let m): "Wi-Fi credentials rejected (\(m)). Re-enter the PIN or restart provisioning."
        }
    }
}
