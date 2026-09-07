from __future__ import annotations

import json
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from turkey_domain_replacer.adapters.fake import FakeCloudProvider, FakeDomainInventorySource
from turkey_domain_replacer.commands import CommandService
from turkey_domain_replacer.db import connect
from turkey_domain_replacer.migrations import migrate
from turkey_domain_replacer.models import DomainInventory, FlowStatus
from turkey_domain_replacer.planner import AwsInventorySnapshot
from turkey_domain_replacer.repository import Repository
from turkey_domain_replacer.worker import Worker

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "aws_inventory.json"


def setup(tmp_path):
    connection = connect(tmp_path / "state.db")
    migrate(connection)
    repository = Repository(connection)
    flow = repository.start_flow(
        "agent@example.com",
        DomainInventory("old.example", ("new.example", "later.example"), "rev-1", NOW),
        NOW,
    )
    cloud = FakeCloudProvider(
        AwsInventorySnapshot.from_dict(json.loads(FIXTURE.read_text(encoding="utf-8")))
    )
    sheet = FakeDomainInventorySource()
    worker = Worker(repository, cloud, sheet, labels=("fun", "joy"), worker_id="worker-1")
    return connection, repository, flow, cloud, sheet, worker


def test_worker_executes_preparation_plan_then_waits_for_manual_backoffice(tmp_path):
    connection, repository, flow, cloud, sheet, worker = setup(tmp_path)
    CommandService(repository).confirm_preparation(flow.id, "agent@example.com", NOW)

    assert worker.run_once(NOW) is True

    assert repository.get_flow(flow.id).status is FlowStatus.AWAITING_BACKOFFICE_CONFIRMATION
    assert sheet.commits == [(flow.id, "old.example", "new.example", "rev-1")]
    stages = [event.stage for event in repository.flow_detail(flow.id).stages]
    assert stages.index(FlowStatus.COMMITTING_SWITCH) < stages.index(
        FlowStatus.AWAITING_BACKOFFICE_CONFIRMATION
    )
    assert len(cloud.applied) == 8
    assert [call[0] for call in cloud.calls[::3]] == ["verify"] * 8
    assert [call[0] for call in cloud.calls[1::3]] == ["apply"] * 8
    assert [call[0] for call in cloud.calls[2::3]] == ["verify"] * 8
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM operations WHERE flow_id = ? AND status = 'SUCCEEDED'", (flow.id,)
        ).fetchone()[0]
        == 9
    )
    assert repository.get_lock().flow_id == flow.id


def test_backoffice_confirmation_commits_sheet_then_opens_cleanup_stage(tmp_path):
    connection, repository, flow, _cloud, sheet, worker = setup(tmp_path)
    service = CommandService(repository)
    service.confirm_preparation(flow.id, "agent@example.com", NOW)
    worker.run_once(NOW)
    service.confirm_backoffice_switch(flow.id, "agent@example.com", NOW)

    assert worker.run_once(NOW) is True

    assert sheet.commits == [(flow.id, "old.example", "new.example", "rev-1")]
    assert repository.get_flow(flow.id).status is FlowStatus.AWAITING_CLEANUP_CONFIRMATION
    assert repository.get_lock().flow_id == flow.id
    snapshots = connection.execute(
        "SELECT phase, snapshot_json, plan_json, plan_digest FROM provider_snapshots "
        "WHERE flow_id = ? ORDER BY id",
        (flow.id,),
    ).fetchall()
    assert [row["phase"] for row in snapshots] == ["PREPARE", "CLEANUP_APPROVAL"]
    assert all(json.loads(row["snapshot_json"])["complete"] for row in snapshots)
    assert json.loads(snapshots[-1]["plan_json"])["operations"]
    assert len(snapshots[-1]["plan_digest"]) == 64


def test_verified_cleanup_success_notifies_and_releases_lock(tmp_path):
    connection, repository, flow, _cloud, _sheet, worker = setup(tmp_path)
    service = CommandService(repository)
    service.confirm_preparation(flow.id, "agent@example.com", NOW)
    worker.run_once(NOW)
    service.confirm_backoffice_switch(flow.id, "agent@example.com", NOW)
    worker.run_once(NOW)
    service.confirm_cleanup(flow.id, "old.example", "agent@example.com", NOW)

    assert worker.run_once(NOW) is True

    assert repository.get_flow(flow.id).status is FlowStatus.SUCCEEDED
    assert repository.get_lock() is None
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM notification_outbox "
            "WHERE kind = 'FLOW_SUCCEEDED' AND flow_id = ?",
            (flow.id,),
        ).fetchone()[0]
        == 1
    )


def test_sheet_commit_is_journaled_and_recovers_after_crash(tmp_path):
    path = tmp_path / "state.db"
    connection, repository, flow, cloud, _sheet, worker = setup(tmp_path)
    service = CommandService(repository)
    service.confirm_preparation(flow.id, "agent@example.com", NOW)

    class CrashAfterCommit(FakeDomainInventorySource):
        def commit_switch(self, *args):
            super().commit_switch(*args)
            raise SystemExit("simulated process death after remote commit")

    crashing_sheet = CrashAfterCommit()
    crashing = Worker(
        repository,
        cloud,
        crashing_sheet,
        labels=("fun", "joy"),
        worker_id="crashing-worker",
        lease_seconds=30,
    )
    with suppress(SystemExit):
        crashing.run_once(NOW)

    recovered_repository = Repository(connect(path))
    recovered = Worker(
        recovered_repository,
        cloud,
        crashing_sheet,
        labels=("fun", "joy"),
        worker_id="recovered-worker",
    )
    assert recovered.run_once(NOW + timedelta(seconds=31)) is True
    assert len(crashing_sheet.commits) == 1
    assert (
        recovered_repository.get_flow(flow.id).status is FlowStatus.AWAITING_BACKOFFICE_CONFIRMATION
    )
    operation = recovered_repository.connection.execute(
        "SELECT status FROM operations WHERE operation_type = 'COMMIT_SHEET_SWITCH'"
    ).fetchone()
    assert operation["status"] == "SUCCEEDED"


def test_sheet_update_failure_never_prompts_backoffice_switch(tmp_path):
    _, repository, flow, cloud, _, _ = setup(tmp_path)

    class FailingSheet(FakeDomainInventorySource):
        def commit_switch(self, *args):
            raise RuntimeError("simulated Sheet update failure")

    CommandService(repository).confirm_preparation(flow.id, "agent@example.com", NOW)
    worker = Worker(repository, cloud, FailingSheet(), labels=("fun", "joy"), worker_id="worker")
    worker.run_once(NOW)
    assert repository.get_flow(flow.id).status is FlowStatus.FAILED_LOCKED
    assert repository.get_lock().flow_id == flow.id
    assert FlowStatus.AWAITING_BACKOFFICE_CONFIRMATION not in [
        event.stage for event in repository.flow_detail(flow.id).stages
    ]


def test_cleanup_refuses_provider_drift_after_user_approved_impact(tmp_path):
    _connection, repository, flow, cloud, _sheet, worker = setup(tmp_path)
    service = CommandService(repository)
    service.confirm_preparation(flow.id, "agent@example.com", NOW)
    worker.run_once(NOW)
    service.confirm_backoffice_switch(flow.id, "agent@example.com", NOW)
    worker.run_once(NOW)
    approved_apply_count = len(cloud.applied)
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["certificates"].append(
        {
            "arn": "arn:aws:acm:us-east-1:111111111111:certificate/newly-observed-old",
            "sans": ["fun.old.example"],
            "in_use_by": [],
        }
    )
    cloud.inventory = AwsInventorySnapshot.from_dict(payload)
    service.confirm_cleanup(flow.id, "old.example", "agent@example.com", NOW)

    assert worker.run_once(NOW) is True
    assert repository.get_flow(flow.id).status is FlowStatus.FAILED_LOCKED
    assert len(cloud.applied) == approved_apply_count
    assert repository.get_lock().flow_id == flow.id
