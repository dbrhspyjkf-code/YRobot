import XCTest
@testable import YRobotRemote

final class MotionCategoryTests: XCTestCase {
    private let cat = MotionCategory()

    func testBasicMembers() {
        for name in ["shake", "nod", "tilt", "surprise", "think", "yawn", "sad", "angry"] {
            XCTAssertEqual(cat.kind(for: name), .basic, "\(name) should be basic")
        }
    }

    func testDanceMembers() {
        let danceNames = [
            "simple_nod", "head_tilt_roll", "dizzy_spin", "jackson_square", "pendulum_swing",
        ]
        for name in danceNames {
            XCTAssertEqual(cat.kind(for: name), .dance, "\(name) should be dance")
        }
    }

    func testUnknownFallsThroughToEmotion() {
        XCTAssertEqual(cat.kind(for: "amazed1"), .emotion)
        XCTAssertEqual(cat.kind(for: "totally-bogus"), .emotion)
    }

    func testChineseNameLookup() {
        XCTAssertEqual(cat.displayName(for: "shake"), "摇头")
        XCTAssertEqual(cat.displayName(for: "dance1"), "跳舞(短)")
        XCTAssertEqual(cat.displayName(for: "unknown"), "unknown")  // falls back
    }

    func testBucketingIsStableAndComplete() {
        let names = ["shake", "nod", "dance1", "dance2", "amazed1", "totally-bogus"]
        let buckets = cat.bucketed(names)
        let total = buckets.reduce(0) { $0 + $1.1.count }
        XCTAssertEqual(total, names.count)
        XCTAssertEqual(buckets.map(\.0), [.basic, .emotion, .dance])
    }
}
