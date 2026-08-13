import Foundation

/// Categorizes motion names into Basic / Emotion / Dance, mirroring the
/// grouping the YRobot dashboard exposes (`MOTION_BASIC` and `MOTION_DANCE`
/// sets in `yrobot/static/main.js`). Anything not in either set is
/// classified as "emotion".
///
/// The dashboard also ships a Chinese display-name table (`MOTION_CN`). We
/// reuse that table here so the buttons stay bilingual — spec §8 Motions
/// says "Preserve the Dashboard's basic, emotion, and dance grouping and
/// Chinese labels".
struct MotionCategory: Sendable {
    let basic: Set<String> = [
        "shake", "nod", "tilt", "surprise", "think", "yawn", "sad", "angry",
    ]
    let dance: Set<String> = [
        "simple_nod", "head_tilt_roll", "side_to_side_sway", "dizzy_spin",
        "stumble_and_recover", "headbanger_combo", "interwoven_spirals",
        "sharp_side_tilt", "side_peekaboo", "yeah_nod", "uh_huh_tilt",
        "neck_recoil", "chin_lead", "groovy_sway_and_roll", "chicken_peck",
        "side_glance_flick", "polyrhythm_combo", "grid_snap", "pendulum_swing",
        "jackson_square",
    ]

    /// Chinese display name table copied verbatim from main.js. Missing
    /// entries fall back to the raw English name.
    let chineseNames: [String: String] = [
        "shake": "摇头", "nod": "点头", "tilt": "歪头", "surprise": "惊喜",
        "think": "思考", "yawn": "打哈欠", "sad": "伤心", "angry": "生气",
        "cheerful1": "愉快", "laughing1": "大笑", "laughing2": "轻笑",
        "surprised1": "惊讶", "surprised2": "惊讶2", "amazed1": "惊叹",
        "thoughtful1": "思考中", "thoughtful2": "思考2", "confused1": "困惑",
        "sad1": "悲伤", "sad2": "悲伤2", "downcast1": "沮丧", "crying": "哭泣",
        "rage1": "愤怒", "furious1": "暴怒", "reprimand1": "训斥", "irritated1": "烦躁",
        "loving1": "喜爱", "shy1": "害羞", "embarrassed": "尴尬",
        "dance1": "跳舞(短)", "dance2": "跳舞(长)", "dance3": "跳舞(活力)",
        "happy": "开心", "yes1": "好的", "no1": "不要", "come1": "过来",
        "welcome1": "欢迎", "welcoming1": "欢迎1", "go_away1": "走开",
        "simple_nod": "点头舞", "head_tilt_roll": "歪头滚", "side_to_side_sway": "左右摆",
        "dizzy_spin": "转圈舞", "stumble_and_recover": "踉跄恢复", "headbanger_combo": "甩头舞",
        "interwoven_spirals": "螺旋舞", "sharp_side_tilt": "侧倾舞", "side_peekaboo": "躲猫猫",
        "yeah_nod": "耶点头", "uh_huh_tilt": "嗯哼歪头", "neck_recoil": "缩脖舞",
        "chin_lead": "下巴领舞", "groovy_sway_and_roll": "律动摇摆", "chicken_peck": "啄木鸟",
        "side_glance_flick": "侧瞥舞", "polyrhythm_combo": "复节奏舞", "grid_snap": "机械定格",
        "pendulum_swing": "钟摆舞", "jackson_square": "杰克逊方步",
    ]

    enum Kind: String, Sendable, CaseIterable, Identifiable {
        case basic, emotion, dance
        var id: String { rawValue }
        var title: String {
            switch self {
            case .basic: "基础动作"
            case .emotion: "常用情绪"
            case .dance: "舞蹈"
            }
        }
    }

    func kind(for name: String) -> Kind {
        if basic.contains(name) { return .basic }
        if dance.contains(name) { return .dance }
        return .emotion
    }

    func displayName(for name: String) -> String {
        chineseNames[name] ?? name
    }

    /// Bucket a flat list of motion names by category. Categories are
    /// always emitted in spec order (basic, emotion, dance) so the UI is
    /// stable regardless of server ordering.
    func bucketed(_ names: [String]) -> [(Kind, [String])] {
        var basic: [String] = []
        var emotion: [String] = []
        var dance: [String] = []
        for n in names {
            switch kind(for: n) {
            case .basic: basic.append(n)
            case .emotion: emotion.append(n)
            case .dance: dance.append(n)
            }
        }
        return [(.basic, basic), (.emotion, emotion), (.dance, dance)]
    }
}
