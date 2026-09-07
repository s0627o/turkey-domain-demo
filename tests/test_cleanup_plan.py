from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from turkey_domain_replacer.models import DomainInventory
from turkey_domain_replacer.planner import AwsInventorySnapshot, build_cleanup_plan

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "aws_inventory.json"


def test_cleanup_plan_removes_only_exact_old_resources_in_safe_order():
    inventory = AwsInventorySnapshot.from_dict(json.loads(FIXTURE.read_text(encoding="utf-8")))
    flow = DomainInventory("old.example", ("new.example",), "rev-1", NOW)

    plan = build_cleanup_plan(flow, inventory, labels=("fun", "joy"))

    assert [(op.kind, op.target) for op in plan.operations] == [
        ("REMOVE_CLOUDFRONT_ALIAS", "DIST-FUN:fun.old.example"),
        ("REMOVE_CLOUDFRONT_ALIAS", "DIST-JOY:joy.old.example"),
        ("DELETE_DNS_RECORD", "ZONE-OLD:fun.old.example:A"),
        ("DELETE_DNS_RECORD", "ZONE-OLD:fun.old.example:AAAA"),
        ("DELETE_DNS_RECORD", "ZONE-OLD:joy.old.example:A"),
        ("DELETE_DNS_RECORD", "ZONE-OLD:joy.old.example:AAAA"),
        ("DELETE_HOSTED_ZONE", "ZONE-OLD"),
        (
            "DELETE_UNUSED_CERTIFICATE",
            "arn:aws:acm:us-east-1:111111111111:certificate/old",
        ),
        ("DISABLE_AUTO_RENEW", "old.example"),
    ]
    assert all("new.example" not in op.target for op in plan.operations)
    assert plan.skipped == ()


def test_shared_certificate_is_retained_and_explained():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["certificates"][0]["in_use_by"] = ["unrelated-listener"]
    inventory = AwsInventorySnapshot.from_dict(data)
    flow = DomainInventory("old.example", ("new.example",), "rev-1", NOW)

    plan = build_cleanup_plan(flow, inventory, labels=("fun", "joy"))

    assert all(op.kind != "DELETE_UNUSED_CERTIFICATE" for op in plan.operations)
    assert plan.skipped == (
        "certificate arn:aws:acm:us-east-1:111111111111:certificate/old is still attached",
    )
