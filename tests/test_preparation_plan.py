from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from turkey_domain_replacer.models import DomainInventory, Flow, FlowStatus
from turkey_domain_replacer.planner import AwsInventorySnapshot, build_preparation_plan

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "aws_inventory.json"


def test_preparation_plan_is_deterministic_and_uses_live_old_alias_mapping():
    inventory = AwsInventorySnapshot.from_dict(json.loads(FIXTURE.read_text(encoding="utf-8")))
    flow = DomainInventory("old.example", ("new.example",), "rev-1", NOW)

    first = build_preparation_plan(flow, inventory, labels=("fun", "joy"))
    second = build_preparation_plan(flow, inventory, labels=("fun", "joy"))

    assert [(op.kind, op.target) for op in first.operations] == [
        ("ENSURE_CERTIFICATE", "new.example"),
        ("ENSURE_CERTIFICATE_VALIDATION", "new.example"),
        ("ENSURE_CLOUDFRONT_ALIAS", "DIST-FUN:fun.new.example"),
        ("ENSURE_DNS_ALIAS", "ZONE-NEW:fun.new.example:A"),
        ("ENSURE_DNS_ALIAS", "ZONE-NEW:fun.new.example:AAAA"),
        ("ENSURE_CLOUDFRONT_ALIAS", "DIST-JOY:joy.new.example"),
        ("ENSURE_DNS_ALIAS", "ZONE-NEW:joy.new.example:A"),
        ("ENSURE_DNS_ALIAS", "ZONE-NEW:joy.new.example:AAAA"),
    ]
    assert [op.idempotency_key for op in first.operations] == [
        op.idempotency_key for op in second.operations
    ]
    assert len(set(op.idempotency_key for op in first.operations)) == len(first.operations)
    assert all("old.example" not in op.target for op in first.operations)


def test_idempotency_keys_are_scoped_to_flow_and_phase():
    inventory = AwsInventorySnapshot.from_dict(json.loads(FIXTURE.read_text(encoding="utf-8")))
    common = {
        "old_domain": "old.example",
        "new_domain": "new.example",
        "actor_email": "agent@example.com",
        "inventory_revision": "rev-1",
        "inventory_observed_at": NOW,
        "status": FlowStatus.PREPARING,
        "note": "",
        "created_at": NOW,
        "updated_at": NOW,
    }
    first = Flow(id="flow-1", **common)
    second = Flow(id="flow-2", **common)

    first_keys = {
        operation.idempotency_key
        for operation in build_preparation_plan(first, inventory, labels=("fun", "joy")).operations
    }
    second_keys = {
        operation.idempotency_key
        for operation in build_preparation_plan(second, inventory, labels=("fun", "joy")).operations
    }

    assert first_keys.isdisjoint(second_keys)
