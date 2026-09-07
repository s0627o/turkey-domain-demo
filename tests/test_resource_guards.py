from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from turkey_domain_replacer.models import DomainInventory
from turkey_domain_replacer.planner import (
    AwsInventorySnapshot,
    UnsafePlan,
    build_cleanup_plan,
    build_preparation_plan,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "aws_inventory.json"


def snapshot() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def flow() -> DomainInventory:
    return DomainInventory("old.example", ("new.example",), "rev-1", NOW)


def test_incomplete_provider_evidence_blocks_both_plans():
    data = snapshot()
    data["complete"] = False
    inventory = AwsInventorySnapshot.from_dict(data)

    with pytest.raises(UnsafePlan, match="incomplete"):
        build_preparation_plan(flow(), inventory, labels=("fun", "joy"))
    with pytest.raises(UnsafePlan, match="incomplete"):
        build_cleanup_plan(flow(), inventory, labels=("fun", "joy"))


def test_ambiguous_old_alias_mapping_blocks_preparation():
    data = snapshot()
    data["distributions"][1]["aliases"].append("fun.old.example")

    with pytest.raises(UnsafePlan, match="exactly one distribution"):
        build_preparation_plan(flow(), AwsInventorySnapshot.from_dict(data), labels=("fun", "joy"))


def test_foreign_record_in_old_zone_blocks_zone_cleanup():
    data = snapshot()
    data["hosted_zones"][0]["records"].append({"name": "mail.old.example", "type": "MX"})

    with pytest.raises(UnsafePlan, match="foreign record"):
        build_cleanup_plan(flow(), AwsInventorySnapshot.from_dict(data), labels=("fun", "joy"))


def test_missing_exact_old_registration_blocks_auto_renew_change():
    data = snapshot()
    data["registered_domains"] = [{"name": "new.example", "auto_renew": True}]

    with pytest.raises(UnsafePlan, match="registered old domain"):
        build_cleanup_plan(flow(), AwsInventorySnapshot.from_dict(data), labels=("fun", "joy"))


def test_old_and_new_domain_must_be_distinct():
    same = DomainInventory("new.example", ("new.example",), "rev-1", NOW)

    with pytest.raises(UnsafePlan, match="distinct"):
        build_cleanup_plan(same, AwsInventorySnapshot.from_dict(snapshot()), labels=("fun", "joy"))
