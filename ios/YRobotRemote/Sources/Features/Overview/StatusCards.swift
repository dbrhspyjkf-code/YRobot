import SwiftUI

/// Small reusable card components for the Overview screen. Per spec §8
/// Overview, each subsystem renders independently — a missing or stale
/// section does not hide unrelated ones.

struct StaleBanner: View {
    let message: String
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.yellow)
            Text(message)
                .font(.footnote)
        }
        .listRowBackground(Color.yellow.opacity(0.12))
    }
}

struct ConnectionCard: View {
    let endpoint: RobotEndpoint
    let availability: RobotSession.Availability
    let lastDaemonFetch: Date?
    let lastYRobotFetch: Date?
    let lastSuccessfulFetch: Date?

    var body: some View {
        Section("Connection") {
            LabeledContent("Endpoint", value: endpoint.displayLabel)
            LabeledContent("Daemon", value: subsystem(availability.daemon, lastFetch: lastDaemonFetch))
            LabeledContent("YRobot", value: subsystem(availability.yrobot, lastFetch: lastYRobotFetch))
            if let lastSuccessfulFetch {
                LabeledContent("Last refresh",
                    value: lastSuccessfulFetch.formatted(date: .omitted, time: .standard))
            }
        }
    }

    private func subsystem(_ s: RobotSession.SubsystemState, lastFetch: Date?) -> String {
        switch s {
        case .unknown: return "—"
        case .probing: return "probing…"
        case .available:
            if let lastFetch {
                return "available (\(lastFetch.formatted(date: .omitted, time: .standard)))"
            }
            return "available"
        case .unavailable(let reason, _):
            return "offline (\(reason))"
        }
    }
}

struct RobotCard: View {
    let status: YRobotStatus
    var body: some View {
        Section("Robot") {
            LabeledContent("Name", value: status.status.daemon.robot_name)
            LabeledContent("Hardware",
                value: String(status.status.daemon.hardware_id.prefix(8)))
            LabeledContent("Daemon state", value: status.status.daemon.daemon_state)
            LabeledContent("Motor mode", value: status.status.daemon.motor_mode)
            LabeledContent("Awake", value: status.status.daemon.awake ? "yes" : "no")
            LabeledContent("App lock", value: status.status.daemon.app_lock_state)
            if let active = status.status.daemon.active_app, !active.isEmpty {
                LabeledContent("Active app", value: active)
            }
        }
    }
}

struct TrackingCard: View {
    let status: YRobotStatus
    var body: some View {
        Section("Tracking & DoA") {
            LabeledContent("Tracking source",
                value: status.status.motion.tracking?.source ?? "—")
            LabeledContent("Face detected",
                value: (status.status.motion.tracking?.face_detected ?? false) ? "yes" : "no")
            if let doa = status.status.daemon.doa_angle_rad {
                LabeledContent("DoA angle", value: String(format: "%.2f rad", doa))
            }
            LabeledContent("Speech detected",
                value: (status.status.daemon.doa_speech_detected ?? false) ? "yes" : "no")
        }
    }
}

struct MotionCard: View {
    let status: YRobotStatus
    var body: some View {
        Section("Motion") {
            LabeledContent("Ready",
                value: status.status.motion.ready ? "yes" : "no")
            LabeledContent("Thread alive",
                value: status.status.motion.thread_alive ? "yes" : "no")
            LabeledContent("Mode", value: status.status.motion.mode)
            if let current = status.status.motion.current_move {
                LabeledContent("Current move", value: current)
            }
            LabeledContent("Loop rate",
                value: String(format: "%.1f Hz", status.status.motion.loop_hz))
            LabeledContent("Deadline misses",
                value: String(status.status.motion.deadline_misses))
            LabeledContent("Antennas",
                value: status.status.motion.antennas
                    .map { String(format: "%.2f", $0) }
                    .joined(separator: ", "))
        }
    }
}

struct AudioCard: View {
    let status: YRobotStatus
    var body: some View {
        Section("Audio") {
            LabeledContent("Volume",
                value: "\(status.status.audio.volume_percent)%")
            LabeledContent("Upload enabled",
                value: status.status.audio.input_enabled ? "yes" : "no")
            if let mic = status.status.audio.mic {
                LabeledContent("Mic level",
                    value: String(format: "%.0f%% (%.0f dB)", mic.level_percent * 100, mic.level_db))
                LabeledContent("Voice active", value: mic.voiced ? "yes" : "no")
            }
            if let err = status.status.audio.volume_error {
                Label("Volume error: \(err)", systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.red)
            }
        }
    }
}

struct SystemCard: View {
    let status: YRobotStatus
    var body: some View {
        Section("System") {
            LabeledContent("CPU",
                value: "\(Int(status.status.system.cpu_percent))%")
            LabeledContent("Memory",
                value: "\(Int(status.status.system.memory_percent))%")
            LabeledContent("Disk",
                value: "\(Int(status.status.system.disk_percent))%")
            LabeledContent("Temperature",
                value: String(format: "%.0f°C", status.status.system.temperature_c))
            if let power = status.status.system.power {
                if power.under_voltage {
                    Label("Under-voltage", systemImage: "bolt.slash")
                        .foregroundStyle(.red)
                }
                if power.throttled {
                    Label("Throttled", systemImage: "tortoise")
                        .foregroundStyle(.orange)
                }
            }
        }
    }
}

struct PrivacyCard: View {
    let status: YRobotStatus
    var body: some View {
        Section("Privacy") {
            LabeledContent("Audio → gateway",
                value: status.status.privacy.audio_uploaded_to_gateway ? "yes" : "no")
            LabeledContent("Video → gateway",
                value: status.status.privacy.video_uploaded_to_gateway ? "yes" : "no")
            LabeledContent("Local recording",
                value: status.status.privacy.local_media_recording ? "yes" : "no")
        }
    }
}

struct ConversationCard: View {
    let turns: [ConversationTurn]

    var body: some View {
        Section("Recent conversation") {
            if turns.isEmpty {
                Text("No recent turns").foregroundStyle(.secondary)
            } else {
                ForEach(turns) { turn in
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: turn.speaker == .user ? "person.circle" : "bubble.left")
                            .foregroundStyle(turn.speaker == .user ? .blue : .orange)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(turn.text)
                                .font(.callout)
                            Text(turn.timestamp.formatted(date: .omitted, time: .standard))
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }
}
