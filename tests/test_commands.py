from __future__ import annotations

from datetime import UTC, datetime

import pytest

from turkey_domain_replacer.commands import CommandService, InvalidTransition
from turkey_domain_replacer.db import connect
from turkey_domain_replacer.migrations import migrate
from turkey_domain_replacer.models import DomainInventory, FlowStatus
from turkey_domain_replacer.repository import Repository

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def setup_flow(tmp_path):
    connection = connect(tmp_path / "state.db")
    migrate(connection)
    repository = Repository(connection)
    flow = repository.start_flow(
        "agent@example.com",
        DomainInventory("old.example", ("new.example", "later.example"), "rev-1", NOW),
        NOW,
    )
    return connection, repository, flow


def test_preparation_confirmation_is_idempotent_and_enqueues_once(tmp_path):
    connection, repository, flow = setup_flow(tmp_path)
    service = CommandService(repository)

    first = service.confirm_preparation(flow.id, "agent@example.com", NOW)
    second = service.confirm_preparation(flow.id, "agent@example.com", NOW)

    assert first.status is FlowStatus.PREPARATION_QUEUED
    assert second.status is FlowStatus.PREPARATION_QUEUED
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE flow_id = ? AND kind = 'PREPARE'", (flow.id,)
        ).fetchone()[0]
        == 1
    )


def test_illegal_transition_does_not_enqueue_job(tmp_path):
    connection, repository, flow = setup_flow(tmp_path)

    with pytest.raises(InvalidTransition, match="cleanup"):
        CommandService(repository).confirm_cleanup(flow.id, "old.example", "agent@example.com", NOW)

    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_cleanup_requires_exact_old_apex_confirmation(tmp_path):
    connection, repository, flow = setup_flow(tmp_path)
    connection.execute(
        "UPDATE flows SET status = ? WHERE id = ?",
        (FlowStatus.AWAITING_CLEANUP_CONFIRMATION.value, flow.id),
    )
    service = CommandService(repository)

    with pytest.raises(ValueError, match="exact old domain"):
        service.confirm_cleanup(flow.id, "new.example", "agent@example.com", NOW)

    queued = service.confirm_cleanup(flow.id, " old.example ", "agent@example.com", NOW)
    assert queued.status is FlowStatus.CLEANUP_QUEUED
