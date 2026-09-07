from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from .db import connect
from .guards import UnsafePlan
from .heartbeat import LeaseHeartbeat
from .models import FlowStatus, Job
from .planner import Operation, build_cleanup_plan, build_preparation_plan
from .repository import LeaseLost, Repository


class CloudProvider(Protocol):
    def inventory_for(self, old_domain: str, new_domain: str): ...

    def verify(self, operation: Operation) -> bool: ...

    def apply(self, operation: Operation, idempotency_key: str) -> None: ...


class DomainInventorySource(Protocol):
    def verify_switch(
        self,
        flow_id: str,
        old_domain: str,
        new_domain: str,
        expected_revision: str,
    ) -> bool: ...

    def commit_switch(
        self,
        flow_id: str,
        old_domain: str,
        new_domain: str,
        expected_revision: str,
        idempotency_key: str,
    ) -> None: ...


class Worker:
    def __init__(
        self,
        repository: Repository,
        cloud: CloudProvider,
        inventory_source: DomainInventorySource,
        *,
        labels: tuple[str, ...],
        worker_id: str,
        lease_seconds: int = 300,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.cloud = cloud
        self.inventory_source = inventory_source
        self.labels = labels
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.clock = clock
        database_row = repository.connection.execute("PRAGMA database_list").fetchone()
        self.database_path = Path(database_row["file"])
        if not str(self.database_path):
            raise ValueError("worker lease heartbeat requires a file-backed SQLite database")

    def run_once(self, now: datetime) -> bool:
        self._run_origin = now
        self._monotonic_origin = time.monotonic()
        job = self.repository.claim_job(self.worker_id, now, lease_seconds=self.lease_seconds)
        if job is None:
            return False
        try:
            self._run_job(job, now)
        except LeaseLost:
            pass
        except Exception as error:
            with suppress(LeaseLost):
                self.repository.fail_job(job, self._now(now), error, worker_id=self.worker_id)
        return True

    def _run_job(self, job: Job, now: datetime) -> None:
        flow = self.repository.get_flow(job.flow_id)
        if job.kind == "PREPARE":
            self.repository.mark_job_running(
                job, FlowStatus.PREPARING, self._now(now), worker_id=self.worker_id
            )
            inventory = self._inventory(job, flow.old_domain, flow.new_domain, now)
            plan = build_preparation_plan(flow, inventory, labels=self.labels)
            self.repository.record_provider_snapshot(
                job, "PREPARE", inventory, plan, self._now(now), worker_id=self.worker_id
            )
            self._execute_operations(job, plan.operations, now)
            self.repository.mark_job_running(
                job, FlowStatus.COMMITTING_SWITCH, self._now(now), worker_id=self.worker_id
            )
            operation = Operation(
                "COMMIT_SHEET_SWITCH",
                f"{flow.old_domain}->{flow.new_domain}",
                {
                    "flow_id": flow.id,
                    "old_domain": flow.old_domain,
                    "new_domain": flow.new_domain,
                    "expected_revision": flow.inventory_revision,
                },
                scope=f"{flow.id}:COMMIT_SWITCH",
            )
            self.repository.ensure_operation(
                job, operation, self._now(now), worker_id=self.worker_id
            )
            already_applied = self._external(
                job,
                now,
                lambda: self.inventory_source.verify_switch(
                    flow.id, flow.old_domain, flow.new_domain, flow.inventory_revision
                ),
            )
            if not already_applied:
                self._external(
                    job,
                    now,
                    lambda: self.inventory_source.commit_switch(
                        flow.id,
                        flow.old_domain,
                        flow.new_domain,
                        flow.inventory_revision,
                        operation.idempotency_key,
                    ),
                )
                if not self._external(
                    job,
                    now,
                    lambda: self.inventory_source.verify_switch(
                        flow.id, flow.old_domain, flow.new_domain, flow.inventory_revision
                    ),
                ):
                    raise RuntimeError("Sheet switch postcondition failed")
            self.repository.complete_operation(
                job,
                operation,
                self._now(now),
                worker_id=self.worker_id,
                already_applied=already_applied,
            )
            self.repository.complete_job(
                job,
                FlowStatus.AWAITING_BACKOFFICE_CONFIRMATION,
                self._now(now),
                worker_id=self.worker_id,
            )
            return
        if job.kind == "BUILD_CLEANUP_PLAN":
            self.repository.mark_job_running(
                job, FlowStatus.PREPARING_CLEANUP_PLAN, self._now(now), worker_id=self.worker_id
            )
            inventory = self._inventory(job, flow.old_domain, flow.new_domain, now)
            cleanup_plan = build_cleanup_plan(flow, inventory, labels=self.labels)
            self.repository.record_provider_snapshot(
                job,
                "CLEANUP_APPROVAL",
                inventory,
                cleanup_plan,
                self._now(now),
                worker_id=self.worker_id,
            )
            self.repository.record_planned_operations(
                job, cleanup_plan.operations, self._now(now), worker_id=self.worker_id
            )
            self.repository.complete_job(
                job,
                FlowStatus.AWAITING_CLEANUP_CONFIRMATION,
                self._now(now),
                worker_id=self.worker_id,
            )
            return
        if job.kind == "CLEANUP":
            self.repository.mark_job_running(
                job, FlowStatus.CLEANING, self._now(now), worker_id=self.worker_id
            )
            inventory = self._inventory(job, flow.old_domain, flow.new_domain, now)
            plan = build_cleanup_plan(flow, inventory, labels=self.labels)
            approved_digest = self.repository.approved_plan_digest(flow.id, "CLEANUP_APPROVAL")
            if plan.digest != approved_digest:
                raise UnsafePlan("cleanup provider state drifted after user approval")
            self._execute_operations(job, plan.operations, now)
            self.repository.complete_job(
                job,
                FlowStatus.SUCCEEDED,
                self._now(now),
                worker_id=self.worker_id,
                final=True,
            )
            return
        raise RuntimeError(f"unknown job kind: {job.kind}")

    def _execute_operations(
        self, job: Job, operations: tuple[Operation, ...], now: datetime
    ) -> None:
        for operation in operations:
            self.repository.ensure_operation(
                job, operation, self._now(now), worker_id=self.worker_id
            )
            if self._external(job, now, lambda operation=operation: self.cloud.verify(operation)):
                self.repository.complete_operation(
                    job,
                    operation,
                    self._now(now),
                    worker_id=self.worker_id,
                    already_applied=True,
                )
                continue
            self._external(
                job,
                now,
                lambda operation=operation: self.cloud.apply(operation, operation.idempotency_key),
            )
            if not self._external(
                job, now, lambda operation=operation: self.cloud.verify(operation)
            ):
                raise RuntimeError(f"provider postcondition failed for {operation.kind}")
            self.repository.complete_operation(
                job,
                operation,
                self._now(now),
                worker_id=self.worker_id,
                already_applied=False,
            )

    def _inventory(self, job: Job, old_domain: str, new_domain: str, now: datetime):
        return self._external(job, now, lambda: self.cloud.inventory_for(old_domain, new_domain))

    def _external(self, job: Job, fallback: datetime, call):
        self._renew(job, fallback)
        with LeaseHeartbeat(
            lambda: self._heartbeat_renew(job, fallback),
            interval_seconds=max(0.1, self.lease_seconds / 3),
        ):
            result = call()
        self._renew(job, fallback)
        return result

    def _heartbeat_renew(self, job: Job, fallback: datetime) -> None:
        connection = connect(self.database_path)
        try:
            Repository(connection).renew_job_lease(
                job,
                self.worker_id,
                self._now(fallback),
                lease_seconds=self.lease_seconds,
            )
        finally:
            connection.close()

    def _renew(self, job: Job, fallback: datetime) -> None:
        self.repository.renew_job_lease(
            job,
            self.worker_id,
            self._now(fallback),
            lease_seconds=self.lease_seconds,
        )

    def _now(self, fallback: datetime) -> datetime:
        if self.clock is not None:
            return self.clock()
        elapsed = time.monotonic() - getattr(self, "_monotonic_origin", time.monotonic())
        return getattr(self, "_run_origin", fallback) + timedelta(seconds=elapsed)


def main() -> None:
    raise SystemExit("Use production runtime wiring only after deployment approval")
