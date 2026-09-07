from __future__ import annotations

import socket

from turkey_domain_replacer.adapters.dry_run import DryRunCloudProvider
from turkey_domain_replacer.planner import AwsInventorySnapshot, Operation


def test_dry_run_path_never_opens_a_network_socket(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network access is forbidden in local dry-run tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    inventory = AwsInventorySnapshot.from_dict(
        {
            "complete": True,
            "distributions": [],
            "certificates": [],
            "hosted_zones": [],
            "registered_domains": [],
        }
    )
    adapter = DryRunCloudProvider(inventory)
    operation = Operation("NOOP", "fixture", {})

    adapter.inventory_for("old.example", "new.example")
    adapter.apply(operation, operation.idempotency_key)
    assert adapter.verify(operation) is True
