from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .db import connect
from .heartbeat import LeaseHeartbeat


class UnsafeNotification(ValueError):
    pass


class Notifier(Protocol):
    def send(self, kind: str, payload: dict[str, object], delivery_id: str) -> None: ...


class NotificationLeaseLost(RuntimeError):
    pass


class OutboxSender:
    def __init__(
        self,
        connection: sqlite3.Connection,
        notifier: Notifier,
        *,
        sender_id: str | None = None,
        lease_seconds: int = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.connection = connection
        self.notifier = notifier
        self.sender_id = sender_id or str(uuid4())
        self.lease_seconds = lease_seconds
        self.clock = clock
        database_row = connection.execute("PRAGMA database_list").fetchone()
        self.database_path = Path(database_row["file"])
        if not str(self.database_path):
            raise ValueError("outbox heartbeat requires a file-backed SQLite database")

    def send_pending(self, now: datetime) -> int:
        self._run_origin = now
        self._monotonic_origin = time.monotonic()
        sent = 0
        while row := self._claim_one(self._now(now)):
            payload = json.loads(row["payload_json"])
            try:
                with LeaseHeartbeat(
                    lambda: self._heartbeat_renew(row["id"], now),
                    interval_seconds=max(0.1, self.lease_seconds / 3),
                ):
                    self.notifier.send(row["kind"], payload, row["dedupe_key"])
            except Exception as error:
                self.connection.execute(
                    """
                    UPDATE notification_outbox
                    SET last_error = ?, lease_owner = NULL, lease_deadline = NULL
                    WHERE id = ? AND lease_owner = ? AND sent_at IS NULL
                    """,
                    (type(error).__name__, row["id"], self.sender_id),
                )
                break
            cursor = self.connection.execute(
                """
                UPDATE notification_outbox
                SET sent_at = ?, last_error = NULL, lease_owner = NULL, lease_deadline = NULL
                WHERE id = ? AND lease_owner = ? AND sent_at IS NULL
                """,
                (self._now(now).isoformat(), row["id"], self.sender_id),
            )
            if cursor.rowcount != 1:
                raise NotificationLeaseLost("outbox lease was lost after delivery")
            sent += 1
        return sent

    def _claim_one(self, now: datetime):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT id, kind, dedupe_key, payload_json FROM notification_outbox
                WHERE sent_at IS NULL
                  AND (lease_owner IS NULL OR lease_deadline <= ?)
                ORDER BY id LIMIT 1
                """,
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                self.connection.execute("COMMIT")
                return None
            deadline = now + timedelta(seconds=self.lease_seconds)
            cursor = self.connection.execute(
                """
                UPDATE notification_outbox SET lease_owner = ?, lease_deadline = ?
                WHERE id = ? AND sent_at IS NULL
                  AND (lease_owner IS NULL OR lease_deadline <= ?)
                """,
                (self.sender_id, deadline.isoformat(), row["id"], now.isoformat()),
            )
            self.connection.execute("COMMIT")
            return row if cursor.rowcount == 1 else None
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def _heartbeat_renew(self, row_id: int, fallback: datetime) -> None:
        now = self._now(fallback)
        deadline = now + timedelta(seconds=self.lease_seconds)
        connection = connect(self.database_path)
        try:
            cursor = connection.execute(
                """
                UPDATE notification_outbox SET lease_deadline = ?
                WHERE id = ? AND lease_owner = ? AND sent_at IS NULL
                  AND lease_deadline >= ?
                """,
                (deadline.isoformat(), row_id, self.sender_id, now.isoformat()),
            )
            if cursor.rowcount != 1:
                raise NotificationLeaseLost("outbox lease is no longer owned")
        finally:
            connection.close()

    def _now(self, fallback: datetime) -> datetime:
        if self.clock is not None:
            return self.clock()
        elapsed = time.monotonic() - getattr(self, "_monotonic_origin", time.monotonic())
        return getattr(self, "_run_origin", fallback) + timedelta(seconds=elapsed)


_SENSITIVE_KEY_PARTS = (
    "credential",
    "password",
    "private_key",
    "raw_response",
    "secret",
    "token",
)


def assert_safe_payload(payload: object) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized_key = str(key).lower()
            if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
                raise UnsafeNotification(f"sensitive notification key: {key}")
            assert_safe_payload(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            assert_safe_payload(value)
    elif isinstance(payload, str) and "bearer " in payload.lower():
        raise UnsafeNotification("notification contains an authorization value")
