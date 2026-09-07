from __future__ import annotations

from ..planner import AwsInventorySnapshot, Operation


class DryRunCloudProvider:
    def __init__(self, inventory: AwsInventorySnapshot) -> None:
        self.inventory = inventory
        self._applied: set[str] = set()
        self.records: list[dict[str, str]] = []

    def inventory_for(self, old_domain: str, new_domain: str) -> AwsInventorySnapshot:
        return self.inventory

    def verify(self, operation: Operation) -> bool:
        return operation.idempotency_key in self._applied

    def apply(self, operation: Operation, idempotency_key: str) -> None:
        if idempotency_key != operation.idempotency_key:
            raise ValueError("idempotency key does not match operation")
        if idempotency_key in self._applied:
            return
        self.records.append(
            {
                "idempotency_key": idempotency_key,
                "kind": operation.kind,
                "target": operation.target,
            }
        )
        self._applied.add(idempotency_key)
