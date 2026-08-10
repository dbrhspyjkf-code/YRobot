import XCTest
@testable import YRobotRemote

final class ConversationTurnTests: XCTestCase {

    // MARK: - Synthetic edge cases

    func testParseUserTurnWithTimestampPrefix() {
        // Real shape from systemd journal: timestamp, priority, logger, marker
        let entry = LogEntry(
            timestamp_us: 1,
            timestamp: "2026-08-10 14:32:53",
            level: "info",
            logger: "__main__",
            pid: 1,
            message: "2026-08-10 14:32:53,541 I __main__: xz stt: 你好世界"
        )
        let turns = ConversationTurn.parse([entry])
        XCTAssertEqual(turns.count, 1)
        XCTAssertEqual(turns[0].speaker, .user)
        XCTAssertEqual(turns[0].text, "你好世界")
    }

    func testParseBotTurn() {
        let entry = LogEntry(
            timestamp_us: 2,
            timestamp: "2026-08-10 14:32:54",
            level: "info",
            logger: "__main__",
            pid: 1,
            message: "2026-08-10 14:32:54,001 I __main__: xz tts text: hello there"
        )
        let turns = ConversationTurn.parse([entry])
        XCTAssertEqual(turns.count, 1)
        XCTAssertEqual(turns[0].speaker, .bot)
        XCTAssertEqual(turns[0].text, "hello there")
    }

    func testIgnoresUnrelatedLogLines() {
        let entries = [
            LogEntry(timestamp_us: 1, timestamp: "2026-08-10 14:32:53", level: "info", logger: "yrobot.motion", pid: 1,
                     message: "2026-08-10 14:32:53,248 I yrobot.motion: DoA muted"),
            LogEntry(timestamp_us: 2, timestamp: "2026-08-10 14:32:54", level: "info", logger: "uvicorn", pid: 1,
                     message: "INFO: 127.0.0.1:50340 - \"GET /api/camera/frame HTTP/1.1\" 200 OK"),
        ]
        XCTAssertTrue(ConversationTurn.parse(entries).isEmpty)
    }

    func testMixedTurnsAreSortedByTimestamp() {
        let entries = [
            LogEntry(timestamp_us: 1, timestamp: "2026-08-10 14:32:55", level: "info", logger: "x", pid: 1,
                     message: "xz tts text: hi back"),
            LogEntry(timestamp_us: 2, timestamp: "2026-08-10 14:32:53", level: "info", logger: "x", pid: 1,
                     message: "xz stt: hi"),
        ]
        let turns = ConversationTurn.parse(entries)
        XCTAssertEqual(turns.count, 2)
        XCTAssertEqual(turns[0].speaker, .user)
        XCTAssertEqual(turns[1].speaker, .bot)
    }

    func testStripsLeadingBracketPrefix() {
        let entry = LogEntry(
            timestamp_us: 1, timestamp: "2026-08-10 14:32:53",
            level: "info", logger: "x", pid: 1,
            message: "[2026-08-10 14:32:53] xz stt: hello"
        )
        let turns = ConversationTurn.parse([entry])
        XCTAssertEqual(turns.first?.text, "hello")
    }

    func testEmptyTextAfterMarkerIsSkipped() {
        let entry = LogEntry(
            timestamp_us: 1, timestamp: "2026-08-10 14:32:53",
            level: "info", logger: "x", pid: 1,
            message: "xz stt:    "
        )
        XCTAssertTrue(ConversationTurn.parse([entry]).isEmpty)
    }

    // MARK: - Real captured data

    /// The single chat line we caught from the live robot — exercises the
    /// parser against real systemd-journal formatting (Chinese transcript).
    func testParsesRealCapturedChatLine() throws {
        let bundle = Bundle(for: Self.self)
        guard let url = bundle.url(forResource: "yrobot_logs_chat", withExtension: "json", subdirectory: "Fixtures") else {
            throw XCTSkip("chat log fixture missing")
        }
        let data = try Data(contentsOf: url)
        let resp = try JSONDecoder().decode(LogsResponse.self, from: data)
        let turns = ConversationTurn.parse(resp.logs)
        XCTAssertFalse(turns.isEmpty, "expected at least one parsed turn from real capture")
        XCTAssertTrue(turns.allSatisfy { $0.speaker == .user || $0.speaker == .bot })
        XCTAssertTrue(turns.allSatisfy { !$0.text.isEmpty })
    }
}
