"""Unit tests for app-level helpers (no hardware, no network)."""

import queue
import threading
import time

import numpy as np

from yrobot.config import Settings
from yrobot.main import Conversation, UplinkPacket
from yrobot.realtime import Delta
from yrobot.turn import QUIET_S, TurnGate


def test_urgent_uplink_discards_stale_backlog():
    packets: queue.Queue[UplinkPacket] = queue.Queue(maxsize=4)
    old = np.zeros(16_000, np.float32)
    for captured_at in (1.0, 2.0, 3.0):
        packets.put(
            UplinkPacket(
                old,
                force_listen=False,
                captured_at=captured_at,
                input_id=f"old-{captured_at}",
            )
        )
    urgent = UplinkPacket(
        np.ones(16_000, np.float32),
        force_listen=True,
        captured_at=4.0,
        input_id="forced",
    )
    Conversation._enqueue_packet(packets, urgent, flush_backlog=True)
    assert packets.qsize() == 1
    assert packets.get_nowait() is urgent


def test_slow_websocket_sender_does_not_block_packet_producer():
    entered = threading.Event()
    release = threading.Event()

    class SlowClient:
        def send_chunk(self, audio, jpeg, force_listen, input_id):
            entered.set()
            release.wait(2.0)

    conversation = object.__new__(Conversation)
    conversation._session_dead = threading.Event()
    conversation._video_kv_est = 0.0
    conversation._turn_lock = threading.Lock()
    conversation._gate = TurnGate()
    packets: queue.Queue[UplinkPacket] = queue.Queue(maxsize=4)
    halt = threading.Event()
    packets.put(
        UplinkPacket(
            np.zeros(16_000, np.float32),
            False,
            time.monotonic(),
            "normal-1",
        )
    )
    sender = threading.Thread(
        target=conversation._send_loop,
        args=(SlowClient(), packets, halt, None),
    )
    sender.start()
    try:
        assert entered.wait(1.0)
        started = time.monotonic()
        Conversation._enqueue_packet(
            packets,
            UplinkPacket(
                np.ones(16_000, np.float32),
                False,
                time.monotonic(),
                "normal-2",
            ),
        )
        assert time.monotonic() - started < 0.05
    finally:
        halt.set()
        release.set()
        sender.join(timeout=2)


def _conversation_without_hardware() -> Conversation:
    class FakeMedia:
        pass

    class FakeMini:
        media = FakeMedia()

    return Conversation(
        Settings(head_tracking_weight=0.0),
        FakeMini(),
        threading.Event(),
    )


def test_barge_candidate_hard_stops_and_latches_force():
    conversation = _conversation_without_hardware()
    started = time.monotonic()
    old_epoch = conversation._speaker.epoch

    conversation._begin_barge(started)

    assert conversation._speaker.epoch == old_epoch + 1
    assert conversation._speaker._flush_event.is_set()
    assert conversation._gate.latched
    with conversation._turn_lock:
        assert conversation._gate.chunk_force_listen(started + 0.01)


def test_interrupted_multi_branch_output_waits_for_force_listen_boundary():
    conversation = _conversation_without_hardware()
    pcm = np.ones(2400, np.float32)
    conversation._on_delta(Delta(kind="audio", audio=pcm, response_id="old"))
    conversation._speaker._q.get_nowait()

    started = time.monotonic()
    conversation._begin_barge(started)
    conversation._on_delta(
        Delta(kind="audio", audio=pcm, response_id="old-branch-2", received_at=started + 0.04)
    )
    conversation._on_delta(
        Delta(kind="audio", audio=pcm, response_id="old-branch-3", received_at=started + 0.06)
    )
    assert conversation._speaker._q.empty()

    with conversation._turn_lock:
        assert conversation._gate.chunk_force_listen(started + 0.08)
        conversation._gate.force_sent("forced-input", started + 0.08)
    conversation._on_delta(Delta(kind="listen", input_id="wrong-input", received_at=started + 0.09))
    assert conversation._gate.latched
    conversation._on_delta(
        Delta(kind="listen", input_id="forced-input", received_at=started + 0.10)
    )
    conversation._on_delta(
        Delta(
            kind="audio",
            audio=pcm,
            response_id="new-answer",
            received_at=started + QUIET_S + 0.10,
        )
    )
    epoch, queued = conversation._speaker._q.get_nowait()
    assert epoch == conversation._speaker.epoch
    assert queued is pcm


def test_late_callbacks_from_rotated_session_cannot_poison_current_session():
    conversation = _conversation_without_hardware()
    conversation._session_sequence = 2
    pcm = np.ones(2400, np.float32)

    conversation._on_delta(Delta(kind="audio", audio=pcm), session_sequence=1)
    conversation._on_closed("late-close", session_sequence=1)

    assert conversation._speaker._q.empty()
    assert not conversation._session_dead.is_set()
