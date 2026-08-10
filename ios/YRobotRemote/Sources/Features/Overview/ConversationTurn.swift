import Foundation

/// One conversation turn derived from a YRobot log line. Parsed from the
/// `xz stt:` and `xz tts text:` markers that the gateway writes to the
/// systemd journal (spec §8 Overview "recent conversation turns").
public struct ConversationTurn: Sendable, Equatable, Identifiable {
    public enum Speaker: String, Sendable, Equatable {
        case user   // matched `xz stt:`
        case bot    // matched `xz tts text:`
    }

    public let timestamp: Date
    public let speaker: Speaker
    public let text: String

    public var id: String { "\(Int(timestamp.timeIntervalSince1970))-\(speaker.rawValue)" }

    public init(timestamp: Date, speaker: Speaker, text: String) {
        self.timestamp = timestamp
        self.speaker = speaker
        self.text = text
    }

    /// Parses log entries into ordered conversation turns.
    /// The server already filters with `?filter=chat`, but we still verify the
    /// marker in case the caller hands us unfiltered logs.
    public static func parse(_ logs: [LogEntry], now: Date = .now) -> [ConversationTurn] {
        var turns: [ConversationTurn] = []
        for entry in logs {
            guard let speaker = speaker(of: entry.message) else { continue }
            let text = extractText(from: entry.message, after: speaker.marker)
            guard let text, !text.isEmpty else { continue }
            let ts = parseTimestamp(entry.timestamp) ?? now
            turns.append(.init(timestamp: ts, speaker: speaker.role, text: text))
        }
        return turns.sorted { $0.timestamp < $1.timestamp }
    }

    // MARK: - Helpers

    private struct Marker {
        let marker: String
        let role: Speaker
    }

    private static let userMarker = Marker(marker: "xz stt:", role: .user)
    private static let botMarker  = Marker(marker: "xz tts text:", role: .bot)

    private static func speaker(of message: String) -> Marker? {
        if let range = message.range(of: userMarker.marker) {
            // Make sure the user marker isn't actually part of the bot marker.
            if message.range(of: botMarker.marker).map({ $0.upperBound <= range.lowerBound }) ?? true {
                return userMarker
            }
        }
        if message.contains(botMarker.marker) { return botMarker }
        return nil
    }

    /// Strip everything up to and including the marker, then trim leading
    /// whitespace and any leftover `[bracketed timestamp]` prefix. Mirrors the
    /// regex the dashboard uses (`/^.*xz stt:\s*/` + `/^\[.*?\]\s*/`).
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
