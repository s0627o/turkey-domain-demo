from __future__ import annotations

import sqlite3

EXPECTED_TABLES = {
    "alert_state",
    "audit_events",
    "flows",
    "global_lock",
    "jobs",
    "notification_outbox",
    "oauth_transactions",
    "operations",
    "provider_snapshots",
    "schema_meta",
    "stage_events",
    "web_sessions",
}

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE flows (
    id TEXT PRIMARY KEY,
    old_domain TEXT NOT NULL,
    new_domain TEXT NOT NULL,
    actor_email TEXT NOT NULL,
    inventory_revision TEXT NOT NULL,
    inventory_observed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (old_domain <> new_domain)
);

CREATE TABLE global_lock (
    lock_name TEXT PRIMARY KEY CHECK (lock_name = 'domain-replacement'),
    flow_id TEXT NOT NULL UNIQUE REFERENCES flows(id) ON DELETE RESTRICT,
    acquired_at TEXT NOT NULL
);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    flow_id TEXT NOT NULL REFERENCES flows(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    lease_owner TEXT,
    lease_deadline TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (flow_id, kind)
);

CREATE TABLE operations (
    id TEXT PRIMARY KEY,
    flow_id TEXT NOT NULL REFERENCES flows(id) ON DELETE RESTRICT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
    phase TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE provider_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id TEXT NOT NULL REFERENCES flows(id) ON DELETE RESTRICT,
    phase TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE (flow_id, phase)
);

CREATE TABLE stage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id TEXT NOT NULL REFERENCES flows(id) ON DELETE RESTRICT,
    stage TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    summary TEXT NOT NULL
);

CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id TEXT REFERENCES flows(id) ON DELETE RESTRICT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE TABLE oauth_transactions (
    state_hash TEXT PRIMARY KEY,
    nonce_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE TABLE web_sessions (
    token_hash TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    csrf_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id TEXT REFERENCES flows(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    last_error TEXT,
    lease_owner TEXT,
    lease_deadline TEXT
);

CREATE TABLE alert_state (
    alert_name TEXT PRIMARY KEY,
    epoch INTEGER NOT NULL,
    is_low INTEGER NOT NULL CHECK (is_low IN (0, 1)),
    last_count INTEGER NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE INDEX idx_flows_created_at ON flows(created_at DESC);
CREATE INDEX idx_jobs_claim ON jobs(status, lease_deadline, created_at);
CREATE INDEX idx_operations_flow ON operations(flow_id, created_at);
CREATE INDEX idx_provider_snapshots_flow ON provider_snapshots(flow_id, phase);
CREATE INDEX idx_stage_events_flow ON stage_events(flow_id, occurred_at);
CREATE INDEX idx_audit_events_flow ON audit_events(flow_id, occurred_at);
CREATE INDEX idx_outbox_pending ON notification_outbox(sent_at, created_at);

CREATE TRIGGER stage_events_no_update
BEFORE UPDATE ON stage_events
BEGIN SELECT RAISE(ABORT, 'stage_events are append-only'); END;
CREATE TRIGGER stage_events_no_delete
BEFORE DELETE ON stage_events
BEGIN SELECT RAISE(ABORT, 'stage_events are append-only'); END;
CREATE TRIGGER audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events are append-only'); END;
CREATE TRIGGER audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events are append-only'); END;
CREATE TRIGGER provider_snapshots_no_update
BEFORE UPDATE ON provider_snapshots
BEGIN SELECT RAISE(ABORT, 'provider_snapshots are append-only'); END;
CREATE TRIGGER provider_snapshots_no_delete
BEFORE DELETE ON provider_snapshots
BEGIN SELECT RAISE(ABORT, 'provider_snapshots are append-only'); END;
"""


def migrate(connection: sqlite3.Connection) -> None:
    current_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if current_version == SCHEMA_VERSION:
        return
    if current_version != 0:
        raise RuntimeError(f"unsupported schema version: {current_version}")

    try:
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            + _SCHEMA
            + "\nINSERT INTO schema_meta(key, value) "
            + f"VALUES ('schema_version', '{SCHEMA_VERSION}');\n"
            + f"PRAGMA user_version = {SCHEMA_VERSION};\n"
            + "COMMIT;"
        )
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
