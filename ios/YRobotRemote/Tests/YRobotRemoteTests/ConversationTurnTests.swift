import XCTest
@testable import YRobotRemote

final class ConversationTurnTests: XCTestCase {

    // MARK: - QWEN markers

    func testParseQwenUserTurn() {
        let entry = LogEntry(
            timestamp_us: 1, timestamp: "2026-08-10 14:32:53",
            level: "info", logger: "x", pid: 1,
            message: "qwen stt: 你好"
        )
        let turns = ConversationTurn.parse([entry])
        XCTAssertEqual(turns.count, 1)
        XCTAssertEqual(turns[0].speaker, .user)
        XCTAssertEqual(turns[0].backend, .qwen)
        XCTAssertEqual(turns[0].text, "你好")
    }

    func testParseQwenBotTurn() {
        let entry = LogEntry(
            timestamp_us: 2, timestamp: "2026-08-10 14:32:54",
            level: "info", logger: "x", pid: 1,
            message: "qwen response: 我也好"
        )
        let turns = ConversationTurn.parse([entry])
        XCTAssertEqual(turns.count, 1)
        XCTAssertEqual(turns[0].speaker, .bot)
        XCTAssertEqual(turns[0].backend, .qwen)
        XCTAssertEqual(turns[0].text, "我也好")
    }

    func testParseXiaozhiTurnsKeepBackend() {
        let entry = LogEntry(
            timestamp_us: 1, timestamp: "2026-08-10 14:32:53",
            level: "info", logger: "x", pid: 1,
            message: "xz stt: hi"
        )
        let turns = ConversationTurn.parse([entry])
        XCTAssertEqual(turns.first?.backend, .xiaozhi)
    }

    func testMixedXIAOZHIAndQWENTurns() {
        let entries = [
            LogEntry(timestamp_us: 1, timestamp: "2026-08-10 14:32:53", level: "info", logger: "x", pid: 1,
                     message: "xz stt: old user turn"),
            LogEntry(timestamp_us: 2, timestamp: "2026-08-10 14:32:55", level: "info", logger: "x", pid: 1,
                     message: "xz tts text: old bot turn"),
            LogEntry(timestamp_us: 3, timestamp: "2026-08-10 14:33:01", level: "info", logger: "x", pid: 1,
                     message: "qwen stt: 新用户"),
            LogEntry(timestamp_us: 4, timestamp: "2026-08-10 14:33:02", level: "info", logger: "x", pid: 1,
                     message: "qwen response: 新机器人"),
        ]
        let turns = ConversationTurn.parse(entries)
        XCTAssertEqual(turns.count, 4)
        XCTAssertEqual(turns.map(\.backend), [.xiaozhi, .xiaozhi, .qwen, .qwen])
        XCTAssertEqual(turns.map(\.speaker), [.user, .bot, .user, .bot])
        XCTAssertEqual(turns.map(\.text), [
            "old user turn", "old bot turn", "新用户", "新机器人",
        ])
    }

    func testTurnIDsAreUniqueForSameSecondSameSpeaker() {
        // Two turns at the same wall-clock second, same speaker, different
        // log lines must yield two distinct ids so SwiftUI lists do not
        // collapse them.
        let entries = [
            LogEntry(timestamp_us: 1001, timestamp: "2026-08-10 14:32:53",
                     level: "info", logger: "x", pid: 1,
                     message: "xz stt: first"),
            LogEntry(timestamp_us: 1002, timestamp: "2026-08-10 14:32:53",
                     level: "info", logger: "x", pid: 1,
                     message: "xz stt: second"),
        ]
        let turns = ConversationTurn.parse(entries)
        XCTAssertEqual(turns.count, 2)
        XCTAssertNotEqual(turns[0].id, turns[1].id,
            "ConversationTurn.id must not collapse to wall-clock second + speaker")
    }

    func testMalformedMessageWithoutMarkerIsSkipped() {
        let entry = LogEntry(
            timestamp_us: 1, timestamp: "2026-08-10 14:32:53",
            level: "info", logger: "x", pid: 1,
            message: "some random log line"
        )
        XCTAssertTrue(ConversationTurn.parse([entry]).isEmpty)
    }

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
