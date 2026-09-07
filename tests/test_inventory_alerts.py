from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from turkey_domain_replacer.db import connect
from turkey_domain_replacer.migrations import migrate
from turkey_domain_replacer.models import DomainInventory
from turkey_domain_replacer.repository import InventoryUnavailable, Repository

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def test_low_stock_is_deduplicated_until_count_recovers_to_two(tmp_path):
    connection = connect(tmp_path / "state.db")
    migrate(connection)
    repository = Repository(connection)

    assert repository.observe_inventory_count(2, NOW) is False
    assert repository.observe_inventory_count(1, NOW + timedelta(minutes=1)) is True
    assert repository.observe_inventory_count(1, NOW + timedelta(minutes=2)) is False
    assert repository.observe_inventory_count(0, NOW + timedelta(minutes=3)) is False
    assert repository.observe_inventory_count(2, NOW + timedelta(minutes=4)) is False
    assert repository.observe_inventory_count(1, NOW + timedelta(minutes=5)) is True

    rows = connection.execute(
        "SELECT dedupe_key, payload_json FROM notification_outbox ORDER BY id"
    ).fetchall()
    assert [row["dedupe_key"] for row in rows] == ["LOW_STOCK:1", "LOW_STOCK:2"]
    assert '"remaining": 1' in rows[0]["payload_json"]


def test_selecting_last_spare_starts_flow_and_alerts_zero_remaining(tmp_path):
    connection = connect(tmp_path / "state.db")
    migrate(connection)
    repository = Repository(connection)
    inventory = DomainInventory("old.example", ("last.example",), "rev-1", NOW)

    flow = repository.start_flow("agent@example.com", inventory, NOW)

    assert flow.new_domain == "last.example"
    payload = connection.execute(
        "SELECT payload_json FROM notification_outbox WHERE kind = 'LOW_STOCK'"
    ).fetchone()["payload_json"]
    assert '"remaining": 0' in payload


def test_no_spare_alerts_zero_but_does_not_take_lock(tmp_path):
    connection = connect(tmp_path / "state.db")
    migrate(connection)
    repository = Repository(connection)
    inventory = DomainInventory("old.example", (), "rev-1", NOW)

    with pytest.raises(InventoryUnavailable, match="no spare"):
        repository.start_flow("agent@example.com", inventory, NOW)

    assert repository.get_lock() is None
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE kind = 'LOW_STOCK'"
        ).fetchone()[0]
        == 1
    )
