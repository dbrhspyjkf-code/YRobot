from yrobot.hermes_workspace_notify import (
    HermesWorkspaceNotificationReceiver,
    sign_workspace_notification,
)


class FakePlayer:
    def __init__(self):
        self.task_ids = []

    def enqueue(self, task_id):
        duplicate = task_id in self.task_ids
        if not duplicate:
            self.task_ids.append(task_id)
        return True, duplicate


def _payload(
    secret="secret", *, timestamp=1_000, nonce="a" * 32, task_id="HX-20260826-220000-abcd"
):
    return {
        "event": "completed",
        "task_id": task_id,
        "timestamp": timestamp,
        "nonce": nonce,
        "signature": sign_workspace_notification(
            secret, event="completed", task_id=task_id, timestamp=timestamp, nonce=nonce
        ),
    }


def test_valid_signed_message_is_queued_once():
    player = FakePlayer()
    receiver = HermesWorkspaceNotificationReceiver("secret", player, now=lambda: 1_000)
    payload = _payload()

    first = receiver.receive("192.168.1.200", payload)
    assert first.status == 202
    assert first.body == {"ok": True, "queued": True, "duplicate": False}
    assert player.task_ids == [payload["task_id"]]

    replay = receiver.receive("192.168.1.200", payload)
    assert replay.status == 409
    assert player.task_ids == [payload["task_id"]]


def test_rejects_wrong_peer_expiry_and_signature():
    player = FakePlayer()
    receiver = HermesWorkspaceNotificationReceiver("secret", player, now=lambda: 1_000)

    assert receiver.receive("192.168.1.99", _payload()).status == 403
    assert receiver.receive("192.168.1.200", _payload(timestamp=800)).status == 401
    bad = _payload()
    bad["signature"] = "0" * 64
    assert receiver.receive("192.168.1.200", bad).status == 401


def test_rejects_unsupported_event_and_task_id():
    player = FakePlayer()
    receiver = HermesWorkspaceNotificationReceiver("secret", player, now=lambda: 1_000)
    bad = _payload(task_id="not-a-task")
    bad["signature"] = sign_workspace_notification(
        "secret", event="completed", task_id="not-a-task", timestamp=1_000, nonce="a" * 32
    )
    assert receiver.receive("192.168.1.200", bad).status == 422


def test_full_queue_does_not_consume_nonce_for_retry():
    class FullPlayer:
        def enqueue(self, _task_id):
            return False, False

    receiver = HermesWorkspaceNotificationReceiver("secret", FullPlayer(), now=lambda: 1_000)
    payload = _payload()
    assert receiver.receive("192.168.1.200", payload).status == 503
    assert receiver.receive("192.168.1.200", payload).status == 503
