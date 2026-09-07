from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from uuid import uuid4

from .models import (
    AuditEvent,
    DomainInventory,
    Flow,
    FlowDetail,
    FlowStatus,
    GlobalLock,
    Job,
    StageEvent,
)
from .notifications import assert_safe_payload


class ActiveFlowExists(RuntimeError):
    pass


class FlowLockUnavailable(ValueError):
    pass


class InventoryUnavailable(RuntimeError):
    pass


class ActiveWorkerLease(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class Repository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def start_flow(self, actor: str, inventory: DomainInventory, now: datetime) -> Flow:
        current, spares = _validate_inventory(inventory)
        if not spares:
            self.observe_inventory_count(0, now)
            raise InventoryUnavailable("no spare domain is available")
        actor_email = actor.strip().lower()
        if not actor_email:
            raise ValueError("actor is required")

        flow_id = str(uuid4())
        status = FlowStatus.AWAITING_PREPARATION_CONFIRMATION
        timestamp = _iso(now)
        with self._transaction():
            lock_row = self.connection.execute(
                "SELECT flow_id FROM global_lock WHERE lock_name = 'domain-replacement'"
            ).fetchone()
            if lock_row is not None:
                raise ActiveFlowExists(f"active flow already holds lock: {lock_row['flow_id']}")
            self.connection.execute(
                """
                INSERT INTO flows(
                    id, old_domain, new_domain, actor_email, inventory_revision,
                    inventory_observed_at, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    flow_id,
                    current,
                    spares[0],
                    actor_email,
                    inventory.revision,
                    _iso(inventory.observed_at),
                    status.value,
                    timestamp,
                    timestamp,
                ),
            )
            self.connection.execute(
                "INSERT INTO global_lock(lock_name, flow_id, acquired_at) VALUES (?, ?, ?)",
                ("domain-replacement", flow_id, timestamp),
            )
            self._insert_stage(flow_id, status, now, "Domain pair reserved for confirmation")
            self._insert_audit(
                flow_id,
                actor_email,
                "FLOW_STARTED",
                now,
                {"old_domain": current, "new_domain": spares[0]},
            )
            self._observe_inventory_count(len(spares) - 1, now)
        return self.get_flow(flow_id)

    def get_lock(self) -> GlobalLock | None:
        row = self.connection.execute(
            "SELECT flow_id, acquired_at FROM global_lock WHERE lock_name = 'domain-replacement'"
        ).fetchone()
        if row is None:
            return None
        return GlobalLock(row["flow_id"], _datetime(row["acquired_at"]))

    def get_flow(self, flow_id: str) -> Flow:
        row = self.connection.execute("SELECT * FROM flows WHERE id = ?", (flow_id,)).fetchone()
        if row is None:
            raise KeyError(flow_id)
        return _flow(row)

    def flow_detail(self, flow_id: str) -> FlowDetail:
        flow = self.get_flow(flow_id)
        stages = tuple(
            StageEvent(FlowStatus(row["stage"]), _datetime(row["occurred_at"]), row["summary"])
            for row in self.connection.execute(
                "SELECT stage, occurred_at, summary FROM stage_events "
                "WHERE flow_id = ? ORDER BY id",
                (flow_id,),
            )
        )
        audit = tuple(
            AuditEvent(
                row["actor"], row["action"], _datetime(row["occurred_at"]), row["detail_json"]
            )
            for row in self.connection.execute(
                "SELECT actor, action, occurred_at, detail_json FROM audit_events "
                "WHERE flow_id = ? ORDER BY id",
                (flow_id,),
            )
        )
        return FlowDetail(flow, stages, audit)

    def history(self) -> tuple[Flow, ...]:
        return tuple(
            _flow(row)
            for row in self.connection.execute(
                "SELECT * FROM flows ORDER BY created_at DESC, id DESC"
            )
        )

    def observe_inventory_count(self, count: int, now: datetime) -> bool:
        if count < 0:
            raise ValueError("inventory count cannot be negative")
        with self._transaction():
            return self._observe_inventory_count(count, now)

    def enqueue_notification(
        self,
        kind: str,
        dedupe_key: str,
        payload: dict[str, object],
        now: datetime,
        flow_id: str | None = None,
    ) -> bool:
        assert_safe_payload(payload)
        with self._transaction():
            return self._enqueue_notification(kind, dedupe_key, payload, now, flow_id)

    def claim_job(self, worker_id: str, now: datetime, *, lease_seconds: int):
        with self._transaction():
            row = self.connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'QUEUED'
                   OR (status = 'RUNNING' AND lease_deadline <= ?)
                ORDER BY created_at, id
                LIMIT 1
                """,
                (_iso(now),),
            ).fetchone()
            if row is None:
                return None
            deadline = now + timedelta(seconds=lease_seconds)
            self.connection.execute(
                """
                UPDATE jobs
                SET status = 'RUNNING', lease_owner = ?, lease_deadline = ?,
                    attempts = attempts + 1, updated_at = ?
                WHERE id = ?
                """,
                (worker_id, _iso(deadline), _iso(now), row["id"]),
            )
            claimed = self.connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            return _job(claimed)

    def renew_job_lease(
        self, job: Job, worker_id: str, now: datetime, *, lease_seconds: int
    ) -> Job:
        deadline = now + timedelta(seconds=lease_seconds)
        with self._transaction():
            cursor = self.connection.execute(
                """
                UPDATE jobs SET lease_deadline = ?, updated_at = ?
                WHERE id = ? AND status = 'RUNNING' AND lease_owner = ?
                  AND lease_deadline >= ?
                """,
                (_iso(deadline), _iso(now), job.id, worker_id, _iso(now)),
            )
            if cursor.rowcount != 1:
                raise LeaseLost(f"job lease is no longer owned by {worker_id}")
            row = self.connection.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
            return _job(row)

    def transition_and_enqueue(
        self,
        flow_id: str,
        *,
        expected: FlowStatus,
        queued: FlowStatus,
        job_kind: str,
        actor: str,
        now: datetime,
        action: str,
    ) -> Flow:
        with self._transaction():
            flow = self.get_flow(flow_id)
            lock = self.get_lock()
            if lock is None or lock.flow_id != flow_id:
                raise FlowLockUnavailable("flow does not hold the global lock")
            if flow.status is queued:
                return flow
            if flow.status is not expected:
                from .commands import InvalidTransition

                raise InvalidTransition(
                    f"cannot {action.lower()} while flow is {flow.status.value}"
                )
            timestamp = _iso(now)
            self.connection.execute(
                "UPDATE flows SET status = ?, updated_at = ? WHERE id = ?",
                (queued.value, timestamp, flow_id),
            )
            self.connection.execute(
                """
                INSERT OR IGNORE INTO jobs(
                    id, flow_id, kind, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'QUEUED', ?, ?)
                """,
                (str(uuid4()), flow_id, job_kind, timestamp, timestamp),
            )
            self._insert_stage(flow_id, queued, now, action)
            self._insert_audit(flow_id, actor.strip().lower(), action, now, {})
        return self.get_flow(flow_id)

    def mark_job_running(
        self, job: Job, status: FlowStatus, now: datetime, *, worker_id: str
    ) -> None:
        with self._transaction():
            self._assert_job_owner(job, worker_id, now)
            self.connection.execute(
                "UPDATE flows SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, _iso(now), job.flow_id),
            )
            self._insert_stage(job.flow_id, status, now, f"Worker started {job.kind}")
            self._insert_audit(job.flow_id, "worker", f"{job.kind}_STARTED", now, {})

    def ensure_operation(self, job: Job, operation, now: datetime, *, worker_id: str) -> str:
        request_json = json.dumps(
            {
                "kind": operation.kind,
                "parameters": operation.parameters,
                "preconditions": operation.preconditions,
                "target": operation.target,
            },
            sort_keys=True,
        )
        with self._transaction():
            self._assert_job_owner(job, worker_id, now)
            self.connection.execute(
                """
                INSERT OR IGNORE INTO operations(
                    id, flow_id, job_id, phase, operation_type, resource_id,
                    idempotency_key, request_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (
                    str(uuid4()),
                    job.flow_id,
                    job.id,
                    job.kind,
                    operation.kind,
                    operation.target,
                    operation.idempotency_key,
                    request_json,
                    _iso(now),
                    _iso(now),
                ),
            )
            row = self.connection.execute(
                "SELECT status FROM operations WHERE idempotency_key = ?",
                (operation.idempotency_key,),
            ).fetchone()
            return row["status"]

    def complete_operation(
        self, job: Job, operation, now: datetime, *, worker_id: str, already_applied: bool
    ) -> None:
        with self._transaction():
            self._assert_job_owner(job, worker_id, now)
            self.connection.execute(
                """
                UPDATE operations
                SET status = 'SUCCEEDED', result_json = ?, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (
                    json.dumps({"already_applied": already_applied}, sort_keys=True),
                    _iso(now),
                    operation.idempotency_key,
                ),
            )

    def record_planned_operations(
        self, job: Job, operations, now: datetime, *, worker_id: str
    ) -> None:
        with self._transaction():
            self._assert_job_owner(job, worker_id, now)
            for operation in operations:
                request_json = json.dumps(
                    {
                        "kind": operation.kind,
                        "parameters": operation.parameters,
                        "preconditions": operation.preconditions,
                        "target": operation.target,
                    },
                    sort_keys=True,
                )
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO operations(
                        id, flow_id, job_id, phase, operation_type, resource_id,
                        idempotency_key, request_json, status, created_at, updated_at
                    ) VALUES (?, ?, ?, 'CLEANUP', ?, ?, ?, ?, 'PLANNED', ?, ?)
                    """,
                    (
                        str(uuid4()),
                        job.flow_id,
                        job.id,
                        operation.kind,
                        operation.target,
                        operation.idempotency_key,
                        request_json,
                        _iso(now),
                        _iso(now),
                    ),
                )

    def planned_operations(self, flow_id: str) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self.connection.execute(
                """
                SELECT operation_type, resource_id, status
                FROM operations
                WHERE flow_id = ? AND phase = 'CLEANUP'
                ORDER BY created_at, id
                """,
                (flow_id,),
            )
        )

    def record_provider_snapshot(
        self,
        job: Job,
        phase: str,
        inventory,
        plan,
        now: datetime,
        *,
        worker_id: str,
    ) -> None:
        with self._transaction():
            self._assert_job_owner(job, worker_id, now)
            self.connection.execute(
                """
                INSERT OR IGNORE INTO provider_snapshots(
                    flow_id, phase, snapshot_json, plan_json, plan_digest, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job.flow_id,
                    phase,
                    json.dumps(inventory.to_dict(), sort_keys=True, separators=(",", ":")),
                    json.dumps(plan.to_dict(), sort_keys=True, separators=(",", ":")),
                    plan.digest,
                    _iso(now),
                ),
            )

    def approved_plan_digest(self, flow_id: str, phase: str) -> str:
        row = self.connection.execute(
            "SELECT plan_digest FROM provider_snapshots WHERE flow_id = ? AND phase = ?",
            (flow_id, phase),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"approved provider plan is missing for {phase}")
        return row["plan_digest"]

    def complete_job(
        self,
        job: Job,
        next_status: FlowStatus,
        now: datetime,
        *,
        worker_id: str,
        final: bool = False,
    ) -> None:
        with self._transaction():
            self._assert_job_owner(job, worker_id, now)
            cursor = self.connection.execute(
                """
                UPDATE jobs SET status = 'SUCCEEDED', lease_owner = NULL,
                    lease_deadline = NULL, updated_at = ?
                WHERE id = ? AND lease_owner = ? AND status = 'RUNNING'
                """,
                (_iso(now), job.id, worker_id),
            )
            if cursor.rowcount != 1:
                raise LeaseLost(f"job lease is no longer owned by {worker_id}")
            self.connection.execute(
                "UPDATE flows SET status = ?, updated_at = ? WHERE id = ?",
                (next_status.value, _iso(now), job.flow_id),
            )
            self._insert_stage(job.flow_id, next_status, now, f"{job.kind} completed")
            self._insert_audit(job.flow_id, "worker", f"{job.kind}_COMPLETED", now, {})
            if final:
                flow = self.get_flow(job.flow_id)
                self._enqueue_notification(
                    "FLOW_SUCCEEDED",
                    f"FLOW_SUCCEEDED:{job.flow_id}",
                    {
                        "new_domain": flow.new_domain,
                        "old_domain": flow.old_domain,
                        "status": next_status.value,
                    },
                    now,
                    job.flow_id,
                )
                self.connection.execute(
                    "DELETE FROM global_lock "
                    "WHERE lock_name = 'domain-replacement' AND flow_id = ?",
                    (job.flow_id,),
                )

    def fail_job(self, job: Job, now: datetime, error: BaseException, *, worker_id: str) -> None:
        error_type = type(error).__name__
        with self._transaction():
            self._assert_job_owner(job, worker_id, now)
            cursor = self.connection.execute(
                """
                UPDATE jobs SET status = 'FAILED', lease_owner = NULL,
                    lease_deadline = NULL, updated_at = ?
                WHERE id = ? AND lease_owner = ? AND status = 'RUNNING'
                """,
                (_iso(now), job.id, worker_id),
            )
            if cursor.rowcount != 1:
                raise LeaseLost(f"job lease is no longer owned by {worker_id}")
            self.connection.execute(
                "UPDATE flows SET status = ?, note = ?, updated_at = ? WHERE id = ?",
                (FlowStatus.FAILED_LOCKED.value, error_type, _iso(now), job.flow_id),
            )
            self._insert_stage(job.flow_id, FlowStatus.FAILED_LOCKED, now, f"{job.kind} failed")
            self._insert_audit(
                job.flow_id,
                "worker",
                f"{job.kind}_FAILED",
                now,
                {"error_type": error_type},
            )
            flow = self.get_flow(job.flow_id)
            self._enqueue_notification(
                "FLOW_FAILED",
                f"FLOW_FAILED:{job.flow_id}",
                {
                    "job_kind": job.kind,
                    "new_domain": flow.new_domain,
                    "old_domain": flow.old_domain,
                    "stage": FlowStatus.FAILED_LOCKED.value,
                },
                now,
                job.flow_id,
            )

    def admin_unlock(
        self,
        flow_id: str,
        operator: str,
        reason: str,
        confirmed_old_domain: str,
        now: datetime,
    ) -> None:
        if not reason.strip() or not operator.strip():
            raise ValueError("operator and reason are required")
        with self._transaction():
            flow = self.get_flow(flow_id)
            if confirmed_old_domain.strip().lower() != flow.old_domain:
                raise ValueError("confirmed old domain does not match")
            lock = self.get_lock()
            if lock is None or lock.flow_id != flow_id:
                raise ValueError("flow does not hold the global lock")
            running = self.connection.execute(
                """
                SELECT id FROM jobs
                WHERE flow_id = ? AND status = 'RUNNING' AND lease_deadline >= ?
                LIMIT 1
                """,
                (flow_id, _iso(now)),
            ).fetchone()
            if running is not None:
                raise ActiveWorkerLease("flow has an active worker lease")
            self.connection.execute(
                "UPDATE flows SET status = ?, note = ?, updated_at = ? WHERE id = ?",
                (FlowStatus.ADMIN_UNLOCKED.value, reason.strip(), _iso(now), flow_id),
            )
            self._insert_stage(
                flow_id, FlowStatus.ADMIN_UNLOCKED, now, "Administrator released lock"
            )
            self._insert_audit(
                flow_id,
                operator.strip(),
                "ADMIN_UNLOCKED",
                now,
                {"reason": reason.strip()},
            )
            self.connection.execute(
                "DELETE FROM global_lock WHERE lock_name = 'domain-replacement' AND flow_id = ?",
                (flow_id,),
            )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def _insert_stage(
        self, flow_id: str, status: FlowStatus, occurred_at: datetime, summary: str
    ) -> None:
        self.connection.execute(
            "INSERT INTO stage_events(flow_id, stage, occurred_at, summary) VALUES (?, ?, ?, ?)",
            (flow_id, status.value, _iso(occurred_at), summary),
        )

    def _assert_job_owner(self, job: Job, worker_id: str, now: datetime) -> None:
        row = self.connection.execute(
            """
            SELECT id FROM jobs
            WHERE id = ? AND status = 'RUNNING' AND lease_owner = ?
              AND lease_deadline >= ?
            """,
            (job.id, worker_id, _iso(now)),
        ).fetchone()
        if row is None:
            raise LeaseLost(f"job lease is no longer owned by {worker_id}")

    def _insert_audit(
        self,
        flow_id: str,
        actor: str,
        action: str,
        occurred_at: datetime,
        detail: dict[str, str],
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(flow_id, actor, action, occurred_at, detail_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (flow_id, actor, action, _iso(occurred_at), json.dumps(detail, sort_keys=True)),
        )

    def _observe_inventory_count(self, count: int, now: datetime) -> bool:
        row = self.connection.execute(
            "SELECT epoch, is_low FROM alert_state WHERE alert_name = 'spare-domain-stock'"
        ).fetchone()
        previous_epoch = row["epoch"] if row is not None else 0
        was_low = bool(row["is_low"]) if row is not None else False
        is_low = count <= 1
        epoch = previous_epoch + 1 if is_low and not was_low else previous_epoch
        self.connection.execute(
            """
            INSERT INTO alert_state(alert_name, epoch, is_low, last_count, observed_at)
            VALUES ('spare-domain-stock', ?, ?, ?, ?)
            ON CONFLICT(alert_name) DO UPDATE SET
                epoch = excluded.epoch,
                is_low = excluded.is_low,
                last_count = excluded.last_count,
                observed_at = excluded.observed_at
            """,
            (epoch, int(is_low), count, _iso(now)),
        )
        if not is_low or was_low:
            return False
        return self._enqueue_notification(
            "LOW_STOCK",
            f"LOW_STOCK:{epoch}",
            {"remaining": count},
            now,
            None,
        )

    def _enqueue_notification(
        self,
        kind: str,
        dedupe_key: str,
        payload: dict[str, object],
        now: datetime,
        flow_id: str | None,
    ) -> bool:
        assert_safe_payload(payload)
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO notification_outbox(
                flow_id, kind, dedupe_key, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (flow_id, kind, dedupe_key, json.dumps(payload, sort_keys=True), _iso(now)),
        )
        return cursor.rowcount == 1


_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _canonical_domain(value: str) -> str:
    domain = value.strip().rstrip(".").lower()
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise InventoryUnavailable("invalid domain") from error
    labels = ascii_domain.split(".")
    if (
        len(labels) < 2
        or len(ascii_domain) > 253
        or any(not _LABEL.fullmatch(label) for label in labels)
    ):
        raise InventoryUnavailable("invalid domain")
    return ascii_domain


def _validate_inventory(inventory: DomainInventory) -> tuple[str, tuple[str, ...]]:
    if not inventory.revision.strip():
        raise InventoryUnavailable("inventory revision is missing")
    current = _canonical_domain(inventory.current_domain)
    spares = tuple(_canonical_domain(domain) for domain in inventory.spare_domains)
    if len(set(spares)) != len(spares):
        raise InventoryUnavailable("duplicate spare domains")
    if current in spares:
        raise InventoryUnavailable("current domain appears in spare inventory")
    return current, spares


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.isoformat()


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _flow(row: sqlite3.Row) -> Flow:
    return Flow(
        id=row["id"],
        old_domain=row["old_domain"],
        new_domain=row["new_domain"],
        actor_email=row["actor_email"],
        inventory_revision=row["inventory_revision"],
        inventory_observed_at=_datetime(row["inventory_observed_at"]),
        status=FlowStatus(row["status"]),
        note=row["note"],
        created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]),
    )


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        flow_id=row["flow_id"],
        kind=row["kind"],
        status=row["status"],
        lease_owner=row["lease_owner"],
        lease_deadline=_datetime(row["lease_deadline"]) if row["lease_deadline"] else None,
        attempts=row["attempts"],
    )
