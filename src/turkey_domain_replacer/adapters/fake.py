from __future__ import annotations

from turkey_domain_replacer.planner import AwsInventorySnapshot, Operation


class FakeCloudProvider:
    def __init__(
        self,
        inventory: AwsInventorySnapshot,
        *,
        already_applied: set[str] | None = None,
        fail_on_kind: str | None = None,
    ) -> None:
        self.inventory = inventory
        self.already_applied = set(already_applied or set())
        self.fail_on_kind = fail_on_kind
        self.applied: list[str] = []
        self.calls: list[tuple[str, str]] = []

    def inventory_for(self, old_domain: str, new_domain: str) -> AwsInventorySnapshot:
        return self.inventory

    def verify(self, operation: Operation) -> bool:
        self.calls.append(("verify", operation.idempotency_key))
        return operation.idempotency_key in self.already_applied

    def apply(self, operation: Operation, idempotency_key: str) -> None:
        self.calls.append(("apply", idempotency_key))
        if operation.kind == self.fail_on_kind:
            raise RuntimeError("simulated provider failure")
        self.applied.append(idempotency_key)
        self.already_applied.add(idempotency_key)


class FakeDomainInventorySource:
    def __init__(self, inventory=None) -> None:
        self.inventory = inventory
        self.commits = []
        self.committed_flow_ids: set[str] = set()

    def read_inventory(self):
        if self.inventory is None:
            raise RuntimeError("fake inventory was not configured")
        return self.inventory

    def commit_switch(
        self,
        flow_id: str,
        old_domain: str,
        new_domain: str,
        expected_revision: str,
        idempotency_key: str,
    ) -> None:
        if flow_id in self.committed_flow_ids:
            return
        self.commits.append((flow_id, old_domain, new_domain, expected_revision))
        self.committed_flow_ids.add(flow_id)

    def verify_switch(
        self,
        flow_id: str,
        old_domain: str,
        new_domain: str,
        expected_revision: str,
    ) -> bool:
        return flow_id in self.committed_flow_ids
