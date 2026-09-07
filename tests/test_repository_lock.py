from __future__ import annotations

from datetime import UTC, datetime

import pytest

from turkey_domain_replacer.db import connect
from turkey_domain_replacer.migrations import migrate
from turkey_domain_replacer.models import DomainInventory, FlowStatus
from turkey_domain_replacer.repository import ActiveFlowExists, InventoryUnavailable, Repository

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def inventory(*spares: str) -> DomainInventory:
    return DomainInventory(
        current_domain="old.example",
        spare_domains=spares,
        revision="sheet-revision-7",
        observed_at=NOW,
    )


def test_first_connection_holds_global_lock_until_terminal_cleanup(tmp_path):
    path = tmp_path / "state.db"
    first_connection = connect(path)
    migrate(first_connection)
    second_connection = connect(path)
    first = Repository(first_connection)
    second = Repository(second_connection)

    flow = first.start_flow("agent@example.com", inventory("next.example", "later.example"), NOW)

    with pytest.raises(ActiveFlowExists, match=flow.id):
        second.start_flow("other@example.com", inventory("next.example"), NOW)
    assert first.get_lock().flow_id == flow.id


def test_start_flow_always_selects_current_and_first_ordered_spare(tmp_path):
    connection = connect(tmp_path / "state.db")
    migrate(connection)

    flow = Repository(connection).start_flow(
        "Agent@Example.com", inventory("first.example", "second.example"), NOW
    )

    assert flow.old_domain == "old.example"
    assert flow.new_domain == "first.example"
    assert flow.actor_email == "agent@example.com"
    assert flow.status is FlowStatus.AWAITING_PREPARATION_CONFIRMATION


def test_zero_available_spares_blocks_flow_without_taking_lock(tmp_path):
    connection = connect(tmp_path / "state.db")
    migrate(connection)
    repository = Repository(connection)

    with pytest.raises(InventoryUnavailable, match="no spare"):
        repository.start_flow("agent@example.com", inventory(), NOW)

    assert repository.get_lock() is None


@pytest.mark.parametrize(
    ("current", "spares"),
    [
        ("", ("next.example",)),
        ("not a domain", ("next.example",)),
        ("old.example", ("next.example", "NEXT.example")),
        ("old.example", ("old.example",)),
    ],
)
def test_invalid_or_ambiguous_inventory_is_rejected(tmp_path, current, spares):
    connection = connect(tmp_path / "state.db")
    migrate(connection)
    repository = Repository(connection)
    bad_inventory = DomainInventory(current, spares, "rev", NOW)

    with pytest.raises(InventoryUnavailable):
        repository.start_flow("agent@example.com", bad_inventory, NOW)

    assert repository.get_lock() is None
