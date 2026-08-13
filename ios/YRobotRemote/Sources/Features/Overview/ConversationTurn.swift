import Foundation

/// One conversation turn derived from a YRobot log line. Parsed from the
/// markers both backends write to the systemd journal:
///
/// XIAOZHI:
///   - user input: `xz stt:`
///   - bot reply:  `xz tts text:`
/// QWEN:
///   - user input: `qwen stt:`
///   - bot reply:  `qwen response:`
///
/// The dashboard filters the journal to chat-only lines via
/// `?filter=chat`, but the iOS parser re-verifies the marker so callers
/// can hand in unfiltered logs safely.
public struct ConversationTurn: Sendable, Equatable, Identifiable {
    public enum Speaker: String, Sendable, Equatable {
        case user   // matched any of xz stt: / qwen stt:
        case bot    // matched any of xz tts text: / qwen response:
    }

    public let id: String
    public let timestamp: Date
    public let speaker: Speaker
    public let text: String
    /// Source backend inferred from the marker. nil when no marker matched
    /// (only possible if a caller hands in unfiltered logs).
    public let backend: ConversationBackend?

    public init(
        id: String,
        timestamp: Date,
        speaker: Speaker,
        text: String,
        backend: ConversationBackend? = nil
    ) {
        self.id = id
        self.timestamp = timestamp
        self.speaker = speaker
        self.text = text
        self.backend = backend
    }

    /// Parses log entries into ordered conversation turns.
    /// The server already filters with `?filter=chat`, but we still verify the
    /// marker in case the caller hands us unfiltered logs.
    public static func parse(_ logs: [LogEntry], now: Date = .now) -> [ConversationTurn] {
        var turns: [ConversationTurn] = []
        for entry in logs {
            guard let marker = markerForEntry(entry.message) else { continue }
            guard let text = extractText(from: entry.message, after: marker.token),
                  !text.isEmpty else { continue }
            let ts = parseTimestamp(entry.timestamp) ?? now
            // Stable id: microsecond timestamp + speaker + backend. Two
            // turns at the same wall-clock second with the same speaker
            // and backend differ by timestamp_us, so list rendering does
            // not collapse them.
            let id = "\(entry.timestamp_us)-\(marker.role.rawValue)-\(marker.backend.rawValue)"
            turns.append(.init(
                id: id,
                timestamp: ts,
                speaker: marker.role,
                text: text,
                backend: marker.backend
            ))
        }
        return turns.sorted { $0.timestamp < $1.timestamp }
    }

    // MARK: - Helpers

    private struct Marker {
        let token: String
        let role: Speaker
        let backend: ConversationBackend
    }

    private static let markers: [Marker] = [
        .init(token: "xz stt:",        role: .user, backend: .xiaozhi),
        .init(token: "xz tts text:",   role: .bot,  backend: .xiaozhi),
        .init(token: "qwen stt:",      role: .user, backend: .qwen),
        .init(token: "qwen response:", role: .bot,  backend: .qwen),
    ]

    private static func markerForEntry(_ message: String) -> Marker? {
        // Prefer the earliest marker in the line. The bot markers are
        // substrings of nothing else in our table, but `xz stt:` and
        // `qwen stt:` are independent and must be checked in order.
        var bestRange: Range<String.Index>?
        var best: Marker?
        for marker in markers {
            if let range = message.range(of: marker.token) {
                if bestRange == nil || range.lowerBound < bestRange!.lowerBound {
                    bestRange = range
                    best = marker
                }
            }
        }
        return best
    }

    /// Strip everything up to and including the marker, then trim leading
    /// whitespace and any leftover `[bracketed timestamp]` prefix. Mirrors the
    /// regex the dashboard uses.
    private static func extractText(from message: String, after marker: String) -> String? {
        guard let range = message.range(of: marker) else { return nil }
        var text = String(message[range.upperBound...])
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if text.hasPrefix("["), let close = text.firstIndex(of: "]") {
            text = String(text[text.index(after: close)...])
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return text.isEmpty ? nil : text
    }

    /// Parse journal "yyyy-MM-dd HH:mm:ss" timestamps. Returns nil on failure
    /// — the caller falls back to "now" so a single bad timestamp doesn't drop
    /// the whole turn.
    private static func parseTimestamp(_ raw: String) -> Date? {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        f.timeZone = TimeZone.current
        f.locale = Locale(identifier: "en_US_POSIX")
        return f.date(from: raw)
    }
}
