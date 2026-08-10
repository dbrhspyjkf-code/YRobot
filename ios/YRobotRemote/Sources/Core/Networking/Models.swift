import Foundation

// MARK: - YRobot dashboard (port 8042)

public struct YRobotStatus: Codable, Sendable, Equatable {
    public let ok: Bool
    public let status: Status

    public struct Status: Codable, Sendable, Equatable {
        public let service: Service
        public let system: SystemHealth
        public let daemon: DaemonHealth
        public let motion: MotionHealth
        public let runtime: RuntimeHealth
        public let conversation: ConversationHealth
        public let audio: AudioHealth
        public let integrations: Integrations
        public let privacy: Privacy
        public let config: Config
    }

    public struct Service: Codable, Sendable, Equatable {
        public let name: String
        public let state: String
        public let pid: Int
        public let uptime_s: Double
    }

    public struct SystemHealth: Codable, Sendable, Equatable {
        public let cpu_percent: Double
        public let memory_percent: Double
        public let disk_percent: Double
        public let temperature_c: Double
        public let power: PowerHealth?

        public struct PowerHealth: Codable, Sendable, Equatable {
            public let available: Bool
            public let under_voltage: Bool
            public let throttled: Bool
            public let frequency_capped: Bool
        }
    }

    public struct DaemonHealth: Codable, Sendable, Equatable {
        public let available: Bool
        public let base_url: String
        public let firmware_version: String
        public let hardware_id: String
        public let robot_name: String
        public let daemon_state: String
        public let motor_mode: String
        public let awake: Bool
        public let app_lock_state: String
        public let active_app: String?
        public let active_app_transport: String?
        public let remote_session_active: Bool?
        public let doa_angle_rad: Double?
        public let doa_speech_detected: Bool?
        public let errors: [String: String]?
    }

    public struct MotionHealth: Codable, Sendable, Equatable {
        public let ready: Bool
        public let thread_alive: Bool
        public let mode: String
        public let current_move: String?
        public let current_recorded: String?
        public let command_queue: Int
        public let loop_hz: Double
        public let deadline_misses: Int
        public let antennas: [Double]
        public let gaze_target_rad: Double?
        public let gaze_source: String?
        public let tracking: TrackingHealth?
    }

    public struct TrackingHealth: Codable, Sendable, Equatable {
        public let source: String?
        public let face_detected: Bool
        public let target_yaw_rad: Double?
        public let audio_yaw_rad: Double?
        public let visual_yaw_rad: Double?
        public let age_s: Double?
    }

    public struct RuntimeHealth: Codable, Sendable, Equatable {
        public let motor_ready: Bool
        public let ws_state: String
        public let session_id: String?
        public let reconnects: Int?
        public let tts_active: Bool
        public let tts_packets: Int?
        public let audio_queue: Int
        public let audio_dropped: Int?
    }

    public struct ConversationHealth: Codable, Sendable, Equatable {
        public let backend: String?
        public let gateway_url: String?
        public let realtime_mode: String?
        public let tls_verify: Bool?
        public let video_enabled: Bool?
        public let proactive_enabled: Bool?
    }

    public struct AudioHealth: Codable, Sendable, Equatable {
        public let volume_percent: Int
        public let control: String
        public let range: [Int]
        public let volume_error: String?
        public let mic: MicHealth?
        public let input_enabled: Bool

        public struct MicHealth: Codable, Sendable, Equatable {
            public let level_db: Double
            public let level_percent: Double
            public let rms: Double
            public let voiced: Bool
            public let available: Bool
        }
    }

    public struct Integrations: Codable, Sendable, Equatable {
        public let home_assistant: HomeAssistant
        public let local_info: LocalInfo

        public struct HomeAssistant: Codable, Sendable, Equatable {
            public let enabled: Bool
            public let configured: Bool
            public let url: String?
            public let whitelist_path: String?
        }

        public struct LocalInfo: Codable, Sendable, Equatable {
            public let enabled: Bool
        }
    }

    public struct Privacy: Codable, Sendable, Equatable {
        public let audio_uploaded_to_gateway: Bool
        public let video_uploaded_to_gateway: Bool
        public let local_media_recording: Bool
    }

    public struct Config: Codable, Sendable, Equatable {
        public let path: String?
        public let environment_overrides: [String]?
    }
}

public struct MotionList: Codable, Sendable, Equatable {
    public let ok: Bool
    public let moves: [String]
    public let current: String?
}

public struct MotionPlayResponse: Codable, Sendable, Equatable {
    public let ok: Bool
    public let message: String?
    public let current: String?
}

public struct MotionPlayRequest: Codable, Sendable, Equatable {
    public let move: String
}

public struct VolumeEnvelope: Codable, Sendable, Equatable {
    public let volume: Volume

    public struct Volume: Codable, Sendable, Equatable {
        public let percent: Int
        public let control: String
        public let range: [Int]
        public let min_percent: Int
        public let max_percent: Int
    }
}

public struct VolumeSetRequest: Codable, Sendable, Equatable {
    public let percent: Int
}

public struct VADEnvelope: Codable, Sendable, Equatable {
    public let vad: VAD

    public struct VAD: Codable, Sendable, Equatable {
        public let rms_min: Double
        public let `min`: Double
        public let `max`: Double
        public let step: Double
        public let unit: String
    }
}

public struct VADSetRequest: Codable, Sendable, Equatable {
    public let rms_min: Double
}

public struct AudioInputEnvelope: Codable, Sendable, Equatable {
    public let audio_input: AudioInput

    public struct AudioInput: Codable, Sendable, Equatable {
        public let enabled: Bool
    }
}

public struct AudioInputSetRequest: Codable, Sendable, Equatable {
    public let enabled: Bool
}

public struct CameraStateEnvelope: Codable, Sendable, Equatable {
    public let state: State

    public struct State: Codable, Sendable, Equatable {
        public let running: Bool
        public let interval_s: Double
        public let long_edge: Int
        public let jpeg_quality: Int
        public let frame_bytes: Int
        public let captured: Int
        public let failures: Int
        public let last_frame_at: Double?
        public let started_at: Double?
    }
}

public struct CameraStateSetRequest: Codable, Sendable, Equatable {
    public let running: Bool
}

public struct SystemServiceStateEnvelope: Codable, Sendable, Equatable {
    public let state: State

    public struct State: Codable, Sendable, Equatable {
        public let service: String
        public let running: Bool
        public let pid: Int
        public let uptime_s: Int
    }
}

public enum SystemPowerAction: String, Codable, Sendable, Equatable {
    case reboot
    case poweroff
}

public struct SystemPowerRequest: Codable, Sendable, Equatable {
    public let action: SystemPowerAction
}

public enum ReachyDaemonAction: String, Codable, Sendable, Equatable {
    case wake
    case sleep
    case restart
}

public struct ReachyDaemonActionRequest: Codable, Sendable, Equatable {
    public let action: ReachyDaemonAction
}

public struct ReachyDaemonActionResponse: Codable, Sendable, Equatable {
    public let ok: Bool
    public let action: String
}

public struct LogEntry: Codable, Sendable, Equatable {
    public let timestamp_us: Int64
    public let timestamp: String
    public let level: String
    public let logger: String
    public let pid: Int
    public let message: String
}

public struct LogsResponse: Codable, Sendable, Equatable {
    public let unit: String
    public let level: String
    public let filter: String
    public let lines: Int
    public let logs: [LogEntry]
}

public enum LogMinLevel: String, Codable, Sendable, Equatable, CaseIterable {
    case debug, info, notice, warning, error
}

public enum LogKindFilter: String, Codable, Sendable, Equatable {
    case chat
}

// MARK: - Reachy daemon (port 8000)

public struct DaemonStatus: Codable, Sendable, Equatable {
    public let type: String
    public let robot_name: String
    public let state: String
    public let wireless_version: Bool
    public let camera_specs_name: String?
    public let backend_status: BackendStatus?
    public let wlan_ip: String?
    public let version: String?
    public let hardware_id: String?
    public let face_target: FaceTarget?

    public struct BackendStatus: Codable, Sendable, Equatable {
        public let ready: Bool
        public let motor_control_mode: String
        public let last_alive: Double?
        public let error: String?
    }

    public struct FaceTarget: Codable, Sendable, Equatable {
        public let detected: Bool
        public let x: Double?
        public let y: Double?
        public let roll: Double?
        public let ts: Double?
    }
}

public enum WifiMode: String, Codable, Sendable, Equatable {
    case hotspot
    case wlan
    case disconnected
    case busy
}

public struct WifiStatus: Codable, Sendable, Equatable {
    public let mode: WifiMode
    public let known_networks: [String]
    public let connected_network: String?
}

public struct WifiError: Codable, Sendable, Equatable {
    public let error: String?
}

public struct WifiResetErrorResponse: Codable, Sendable, Equatable {
    public let status: String
}

public struct WifiForgetRequest: Codable, Sendable, Equatable {
    public let ssid: String
}

public struct WifiProvKey: Codable, Sendable, Equatable {
    public let kid: String
    public let pk: String
    public let alg: String
}

public struct WifiSealedConnectRequest: Codable, Sendable, Equatable {
    public let ssid: String
    public let kid: String
    public let epk: String
    public let nonce: String
    public let ct: String
}

// MARK: - FastAPI error envelope

public struct FastAPIError: Codable, Sendable, Equatable {
    public let detail: String
}
