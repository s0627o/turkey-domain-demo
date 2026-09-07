from __future__ import annotations

from turkey_domain_replacer.db import connect
from turkey_domain_replacer.migrations import EXPECTED_TABLES, migrate


def test_connection_enables_foreign_keys_and_busy_timeout(tmp_path):
    connection = connect(tmp_path / "state.db")

    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] >= 5_000


def test_migration_creates_exact_version_one_schema(tmp_path):
    connection = connect(tmp_path / "state.db")
    migrate(connection)

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }

    assert tables == EXPECTED_TABLES
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert {row[1] for row in connection.execute("PRAGMA table_info(notification_outbox)")} >= {
        "lease_owner",
        "lease_deadline",
    }


def test_migration_is_idempotent(tmp_path):
    connection = connect(tmp_path / "state.db")

    migrate(connection)
    migrate(connection)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
