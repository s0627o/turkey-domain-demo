from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from turkey_domain_replacer.adapters.fake import FakeCloudProvider, FakeDomainInventorySource
from turkey_domain_replacer.commands import CommandService
from turkey_domain_replacer.db import connect
from turkey_domain_replacer.migrations import migrate
from turkey_domain_replacer.models import DomainInventory, FlowStatus
from turkey_domain_replacer.planner import AwsInventorySnapshot, build_preparation_plan
from turkey_domain_replacer.repository import Repository
from turkey_domain_replacer.worker import Worker

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "aws_inventory.json"


def make_state(tmp_path):
    path = tmp_path / "state.db"
    connection = connect(path)
    migrate(connection)
    repository = Repository(connection)
    flow = repository.start_flow(
        "agent@example.com",
        DomainInventory("old.example", ("new.example", "later.example"), "rev-1", NOW),
        NOW,
    )
    CommandService(repository).confirm_preparation(flow.id, "agent@example.com", NOW)
    inventory = AwsInventorySnapshot.from_dict(json.loads(FIXTURE.read_text(encoding="utf-8")))
    return path, connection, repository, flow, inventory


def test_expired_job_lease_is_reclaimed_after_service_restart(tmp_path):
    path, first_connection, first_repository, flow, inventory = make_state(tmp_path)
    claimed = first_repository.claim_job("dead-worker", NOW, lease_seconds=30)
    assert claimed is not None
    first_connection.close()
    second_repository = Repository(connect(path))
    cloud = FakeCloudProvider(inventory)
    restarted = Worker(
        second_repository,
        cloud,
        FakeDomainInventorySource(),
        labels=("fun", "joy"),
        worker_id="restarted-worker",
    )

    assert restarted.run_once(NOW + timedelta(seconds=31)) is True
    assert second_repository.get_flow(flow.id).status is FlowStatus.AWAITING_BACKOFFICE_CONFIRMATION


def test_already_applied_operation_is_verified_without_second_apply(tmp_path):
    _path, _connection, repository, flow, inventory = make_state(tmp_path)
    plan = build_preparation_plan(repository.get_flow(flow.id), inventory, labels=("fun", "joy"))
    cloud = FakeCloudProvider(inventory, already_applied={plan.operations[0].idempotency_key})
    worker = Worker(
        repository,
        cloud,
        FakeDomainInventorySource(),
        labels=("fun", "joy"),
        worker_id="worker-1",
    )

    worker.run_once(NOW)

    assert plan.operations[0].idempotency_key not in cloud.applied
    assert len(cloud.applied) == 7


def test_partial_provider_failure_keeps_lock_and_notifies_once(tmp_path):
    _path, connection, repository, flow, inventory = make_state(tmp_path)
    cloud = FakeCloudProvider(inventory, fail_on_kind="ENSURE_DNS_ALIAS")
    worker = Worker(
        repository,
        cloud,
        FakeDomainInventorySource(),
        labels=("fun", "joy"),
        worker_id="worker-1",
    )

    assert worker.run_once(NOW) is True

    assert repository.get_flow(flow.id).status is FlowStatus.FAILED_LOCKED
    assert repository.get_lock().flow_id == flow.id
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE kind = 'FLOW_FAILED' AND flow_id = ?",
            (flow.id,),
        ).fetchone()[0]
        == 1
    )
    assert worker.run_once(NOW) is False


def test_reclaimed_job_prevents_stale_worker_from_completing_or_failing_flow(tmp_path):
    path, _connection, first_repository, flow, inventory = make_state(tmp_path)
    second_repository = Repository(connect(path))
    current = [NOW]
    second_worker = None

    class ReentrantCloud(FakeCloudProvider):
        def __init__(self):
            super().__init__(inventory)
            self.triggered = False

        def apply(self, operation, idempotency_key):
            if not self.triggered:
                self.triggered = True
                current[0] = NOW + timedelta(seconds=31)
                assert second_worker.run_once(current[0]) is True
            super().apply(operation, idempotency_key)

    cloud = ReentrantCloud()
    first_worker = Worker(
        first_repository,
        cloud,
        FakeDomainInventorySource(),
        labels=("fun", "joy"),
        worker_id="worker-1",
        lease_seconds=30,
        clock=lambda: current[0],
    )
    second_worker = Worker(
        second_repository,
        cloud,
        FakeDomainInventorySource(),
        labels=("fun", "joy"),
        worker_id="worker-2",
        lease_seconds=30,
        clock=lambda: current[0],
    )

    assert first_worker.run_once(NOW) is True
    assert second_repository.get_flow(flow.id).status is FlowStatus.AWAITING_BACKOFFICE_CONFIRMATION
    completed = second_repository.connection.execute(
        "SELECT COUNT(*) FROM audit_events WHERE flow_id = ? AND action = 'PREPARE_COMPLETED'",
        (flow.id,),
    ).fetchone()[0]
    failed = second_repository.connection.execute(
        "SELECT COUNT(*) FROM audit_events WHERE flow_id = ? AND action = 'PREPARE_FAILED'",
        (flow.id,),
    ).fetchone()[0]
    assert completed == 1
    assert failed == 0


def test_worker_heartbeats_lease_during_slow_provider_mutation(tmp_path):
    path, seed_connection, _repository, flow, inventory = make_state(tmp_path)
    seed_connection.close()
    entered = threading.Event()
    release = threading.Event()
    started = time.monotonic()

    def clock():
        return NOW + timedelta(seconds=time.monotonic() - started)

    class SlowCloud(FakeCloudProvider):
        def __init__(self):
            super().__init__(inventory)
            self.apply_counts = {}

        def apply(self, operation, idempotency_key):
            self.apply_counts[idempotency_key] = self.apply_counts.get(idempotency_key, 0) + 1
            entered.set()
            assert release.wait(timeout=4)
            super().apply(operation, idempotency_key)

    cloud = SlowCloud()

    def run_first_worker():
        connection = connect(path)
        try:
            return Worker(
                Repository(connection),
                cloud,
                FakeDomainInventorySource(),
                labels=("fun", "joy"),
                worker_id="slow-worker",
                lease_seconds=1,
                clock=clock,
            ).run_once(clock())
        finally:
            connection.close()

    first_thread = threading.Thread(target=run_first_worker)
    first_thread.start()
    assert entered.wait(timeout=2)
    assert threading.Event().wait(timeout=1.25) is False
    second_connection = connect(path)
    second_worker = Worker(
        Repository(second_connection),
        cloud,
        FakeDomainInventorySource(),
        labels=("fun", "joy"),
        worker_id="second-worker",
        lease_seconds=1,
        clock=clock,
    )

    assert second_worker.run_once(clock()) is False
    release.set()
    first_thread.join(timeout=4)
    assert not first_thread.is_alive()
    assert set(cloud.apply_counts.values()) == {1}
    assert (
        Repository(second_connection).get_flow(flow.id).status
        is FlowStatus.AWAITING_BACKOFFICE_CONFIRMATION
    )
