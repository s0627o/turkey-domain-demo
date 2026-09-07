from __future__ import annotations

import json
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import pytest

from turkey_domain_replacer.db import connect
from turkey_domain_replacer.migrations import migrate
from turkey_domain_replacer.notifications import OutboxSender, UnsafeNotification
from turkey_domain_replacer.repository import Repository

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class RecordingNotifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[tuple[str, dict[str, object]]] = []
        self.delivery_ids: list[str] = []

    def send(self, kind: str, payload: dict[str, object], delivery_id: str) -> None:
        if self.fail:
            raise RuntimeError("provider included a sensitive raw response")
        self.messages.append((kind, payload))
        self.delivery_ids.append(delivery_id)


def test_outbox_sends_each_dedupe_key_once(tmp_path):
    connection = connect(tmp_path / "state.db")
    migrate(connection)
    repository = Repository(connection)
    repository.enqueue_notification(
        "FLOW_SUCCEEDED",
        "FLOW_SUCCEEDED:flow-1",
        {"old_domain": "old.example", "new_domain": "next.example"},
        NOW,
    )
    notifier = RecordingNotifier()
    sender = OutboxSender(connection, notifier)

    assert sender.send_pending(NOW) == 1
    assert sender.send_pending(NOW) == 0
    assert notifier.messages == [
        (
            "FLOW_SUCCEEDED",
            {"new_domain": "next.example", "old_domain": "old.example"},
        )
    ]


def test_failed_send_stays_pending_without_persisting_exception_message(tmp_path):
    connection = connect(tmp_path / "state.db")
    migrate(connection)
    repository = Repository(connection)
    repository.enqueue_notification(
        "FLOW_FAILED", "FLOW_FAILED:flow-1", {"stage": "PREPARING"}, NOW
    )

    assert OutboxSender(connection, RecordingNotifier(fail=True)).send_pending(NOW) == 0

    row = connection.execute(
        "SELECT sent_at, last_error, payload_json FROM notification_outbox"
    ).fetchone()
    assert row["sent_at"] is None
    assert row["last_error"] == "RuntimeError"
    assert json.loads(row["payload_json"]) == {"stage": "PREPARING"}


def test_concurrent_outbox_senders_cannot_send_the_same_row(tmp_path):
    path = tmp_path / "state.db"
    first_connection = connect(path)
    migrate(first_connection)
    Repository(first_connection).enqueue_notification(
        "FLOW_SUCCEEDED", "FLOW_SUCCEEDED:flow-1", {"status": "SUCCEEDED"}, NOW
    )
    second_connection = connect(path)
    calls = []

    class ReentrantNotifier:
        def send(self, kind, payload, delivery_id):
            calls.append((kind, payload))
            assert (
                OutboxSender(
                    second_connection,
                    RecordingNotifier(),
                    sender_id="sender-2",
                    lease_seconds=30,
                ).send_pending(NOW + timedelta(seconds=1))
                == 0
            )

    sender = OutboxSender(
        first_connection, ReentrantNotifier(), sender_id="sender-1", lease_seconds=30
    )

    assert sender.send_pending(NOW) == 1
    assert calls == [("FLOW_SUCCEEDED", {"status": "SUCCEEDED"})]


def test_outbox_heartbeats_during_slow_delivery(tmp_path):
    path = tmp_path / "state.db"
    seed = connect(path)
    migrate(seed)
    Repository(seed).enqueue_notification(
        "FLOW_SUCCEEDED", "FLOW_SUCCEEDED:flow-1", {"status": "SUCCEEDED"}, NOW
    )
    seed.close()
    entered = threading.Event()
    release = threading.Event()
    started = time.monotonic()

    def clock():
        return NOW + timedelta(seconds=time.monotonic() - started)

    deliveries = []

    class SlowNotifier:
        def send(self, kind, payload, delivery_id):
            deliveries.append(delivery_id)
            entered.set()
            assert release.wait(timeout=4)

    def run_first_sender():
        connection = connect(path)
        try:
            return OutboxSender(
                connection,
                SlowNotifier(),
                sender_id="slow-sender",
                lease_seconds=1,
                clock=clock,
            ).send_pending(clock())
        finally:
            connection.close()

    first_thread = threading.Thread(target=run_first_sender)
    first_thread.start()
    assert entered.wait(timeout=2)
    assert threading.Event().wait(timeout=1.25) is False
    second_connection = connect(path)
    assert (
        OutboxSender(
            second_connection,
            RecordingNotifier(),
            sender_id="second-sender",
            lease_seconds=1,
            clock=clock,
        ).send_pending(clock())
        == 0
    )
    release.set()
    first_thread.join(timeout=4)
    assert not first_thread.is_alive()
    assert deliveries == ["FLOW_SUCCEEDED:flow-1"]


def test_outbox_reuses_delivery_id_after_post_send_crash(tmp_path):
    path = tmp_path / "state.db"
    first_connection = connect(path)
    migrate(first_connection)
    Repository(first_connection).enqueue_notification(
        "FLOW_SUCCEEDED", "FLOW_SUCCEEDED:flow-1", {"status": "SUCCEEDED"}, NOW
    )
    delivered = set()
    visible_messages = []

    class IdempotentCrashNotifier:
        crash = True

        def send(self, kind, payload, delivery_id):
            if delivery_id not in delivered:
                delivered.add(delivery_id)
                visible_messages.append((kind, payload))
            if self.crash:
                self.crash = False
                raise SystemExit("process died after provider accepted delivery")

    notifier = IdempotentCrashNotifier()
    first = OutboxSender(first_connection, notifier, sender_id="sender-1", lease_seconds=1)
    with suppress(SystemExit):
        first.send_pending(NOW)

    second = OutboxSender(connect(path), notifier, sender_id="sender-2", lease_seconds=1)
    assert second.send_pending(NOW + timedelta(seconds=2)) == 1
    assert visible_messages == [("FLOW_SUCCEEDED", {"status": "SUCCEEDED"})]
    assert delivered == {"FLOW_SUCCEEDED:flow-1"}


@pytest.mark.parametrize(
    "payload",
    [
        {"token": "abc"},
        {"nested": {"client_secret": "abc"}},
        {"detail": "Bearer secret-value"},
        {"raw_response": {"anything": "value"}},
    ],
)
def test_notification_payload_rejects_sensitive_fields(tmp_path, payload):
    connection = connect(tmp_path / "state.db")
    migrate(connection)

    with pytest.raises(UnsafeNotification):
        Repository(connection).enqueue_notification("FLOW_FAILED", "dedupe", payload, NOW)
