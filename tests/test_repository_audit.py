from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from turkey_domain_replacer.db import connect
from turkey_domain_replacer.migrations import migrate
from turkey_domain_replacer.models import DomainInventory, FlowStatus
from turkey_domain_replacer.repository import Repository

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def make_inventory(current: str, spare: str, revision: str) -> DomainInventory:
    return DomainInventory(current, (spare,), revision, NOW)


def test_start_records_initial_stage_and_sanitized_audit(tmp_path):
    connection = connect(tmp_path / "state.db")
    migrate(connection)
    repository = Repository(connection)

    flow = repository.start_flow(
        "agent@example.com", make_inventory("old.example", "next.example", "rev-1"), NOW
    )
    detail = repository.flow_detail(flow.id)

    assert [(event.stage, event.occurred_at) for event in detail.stages] == [
        (FlowStatus.AWAITING_PREPARATION_CONFIRMATION, NOW)
    ]
    assert [(event.actor, event.action) for event in detail.audit] == [
        ("agent@example.com", "FLOW_STARTED")
    ]
    assert "rev-1" not in detail.audit[0].detail_json


def test_history_is_newest_first_and_contains_required_columns(tmp_path):
    connection = connect(tmp_path / "state.db")
    migrate(connection)
    repository = Repository(connection)
    first = repository.start_flow(
        "first@example.com", make_inventory("one.example", "two.example", "rev-1"), NOW
    )
    repository.admin_unlock(first.id, "operator", "fixture reconciled", "one.example", NOW)
    second = repository.start_flow(
        "second@example.com",
        make_inventory("two.example", "three.example", "rev-2"),
        NOW + timedelta(minutes=1),
    )

    history = repository.history()

    assert [row.id for row in history] == [second.id, first.id]
    assert history[0].old_domain == "two.example"
    assert history[0].new_domain == "three.example"
    assert history[0].actor_email == "second@example.com"
    assert history[0].status is FlowStatus.AWAITING_PREPARATION_CONFIRMATION
    assert history[0].note == ""


def test_stage_and_audit_rows_are_append_only_at_database_boundary(tmp_path):
    connection = connect(tmp_path / "state.db")
    migrate(connection)
    repository = Repository(connection)
    flow = repository.start_flow(
        "agent@example.com", make_inventory("old.example", "next.example", "rev-1"), NOW
    )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "UPDATE stage_events SET summary = 'changed' WHERE flow_id = ?", (flow.id,)
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM audit_events WHERE flow_id = ?", (flow.id,))
