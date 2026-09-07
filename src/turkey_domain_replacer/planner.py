from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Protocol

from .guards import UnsafePlan, require_complete, require_distinct, require_exact_zone


@dataclass(frozen=True)
class Distribution:
    id: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Certificate:
    arn: str
    sans: tuple[str, ...]
    in_use_by: tuple[str, ...]


@dataclass(frozen=True)
class DnsRecord:
    name: str
    type: str


@dataclass(frozen=True)
class HostedZone:
    id: str
    apex: str
    records: tuple[DnsRecord, ...]


@dataclass(frozen=True)
class RegisteredDomain:
    name: str
    auto_renew: bool


@dataclass(frozen=True)
class AwsInventorySnapshot:
    complete: bool
    distributions: tuple[Distribution, ...]
    certificates: tuple[Certificate, ...]
    hosted_zones: tuple[HostedZone, ...]
    registered_domains: tuple[RegisteredDomain, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AwsInventorySnapshot:
        return cls(
            complete=bool(data.get("complete")),
            distributions=tuple(
                Distribution(item["id"], tuple(_domain(value) for value in item["aliases"]))
                for item in data.get("distributions", [])
            ),
            certificates=tuple(
                Certificate(
                    item["arn"],
                    tuple(_domain(value) for value in item["sans"]),
                    tuple(item["in_use_by"]),
                )
                for item in data.get("certificates", [])
            ),
            hosted_zones=tuple(
                HostedZone(
                    item["id"],
                    _domain(item["apex"]),
                    tuple(
                        DnsRecord(_domain(record["name"]), record["type"].upper())
                        for record in item["records"]
                    ),
                )
                for item in data.get("hosted_zones", [])
            ),
            registered_domains=tuple(
                RegisteredDomain(_domain(item["name"]), bool(item["auto_renew"]))
                for item in data.get("registered_domains", [])
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "complete": self.complete,
            "distributions": [
                {"id": item.id, "aliases": list(item.aliases)} for item in self.distributions
            ],
            "certificates": [
                {
                    "arn": item.arn,
                    "sans": list(item.sans),
                    "in_use_by": list(item.in_use_by),
                }
                for item in self.certificates
            ],
            "hosted_zones": [
                {
                    "id": item.id,
                    "apex": item.apex,
                    "records": [
                        {"name": record.name, "type": record.type} for record in item.records
                    ],
                }
                for item in self.hosted_zones
            ],
            "registered_domains": [
                {"name": item.name, "auto_renew": item.auto_renew}
                for item in self.registered_domains
            ],
        }


@dataclass(frozen=True)
class Operation:
    kind: str
    target: str
    parameters: dict[str, object]
    preconditions: tuple[str, ...] = ()
    scope: str = ""

    @property
    def idempotency_key(self) -> str:
        body = json.dumps(
            {
                "kind": self.kind,
                "parameters": self.parameters,
                "preconditions": self.preconditions,
                "scope": self.scope,
                "target": self.target,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True)
class Plan:
    operations: tuple[Operation, ...]
    skipped: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "operations": [
                {
                    "kind": item.kind,
                    "target": item.target,
                    "parameters": item.parameters,
                    "preconditions": list(item.preconditions),
                    "scope": item.scope,
                }
                for item in self.operations
            ],
            "skipped": list(self.skipped),
        }

    @property
    def digest(self) -> str:
        body = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()


class DomainPair(Protocol):
    old_domain: str
    new_domain: str


def build_preparation_plan(
    flow: object, inventory: AwsInventorySnapshot, *, labels: tuple[str, ...]
) -> Plan:
    old_domain, new_domain = _pair(flow)
    _validate_common(old_domain, new_domain, inventory)
    new_zone = require_exact_zone(inventory.hosted_zones, new_domain)
    aliases = tuple(f"{label}.{new_domain}" for label in labels)
    operations = [
        Operation("ENSURE_CERTIFICATE", new_domain, {"sans": list(aliases)}),
        Operation(
            "ENSURE_CERTIFICATE_VALIDATION",
            new_domain,
            {"hosted_zone_id": new_zone.id},
            ("certificate-requested",),
        ),
    ]
    for label in labels:
        old_alias = f"{label}.{old_domain}"
        new_alias = f"{label}.{new_domain}"
        distribution = _exact_alias_distribution(inventory, old_alias)
        conflicting = [
            item.id
            for item in inventory.distributions
            if item.id != distribution.id and new_alias in item.aliases
        ]
        if conflicting:
            raise UnsafePlan(f"new alias is already attached elsewhere: {new_alias}")
        operations.extend(
            [
                Operation(
                    "ENSURE_CLOUDFRONT_ALIAS",
                    f"{distribution.id}:{new_alias}",
                    {"distribution_id": distribution.id, "alias": new_alias},
                    (f"old-alias:{old_alias}", "certificate-issued"),
                ),
                Operation(
                    "ENSURE_DNS_ALIAS",
                    f"{new_zone.id}:{new_alias}:A",
                    {"hosted_zone_id": new_zone.id, "name": new_alias, "type": "A"},
                    (f"distribution:{distribution.id}",),
                ),
                Operation(
                    "ENSURE_DNS_ALIAS",
                    f"{new_zone.id}:{new_alias}:AAAA",
                    {"hosted_zone_id": new_zone.id, "name": new_alias, "type": "AAAA"},
                    (f"distribution:{distribution.id}",),
                ),
            ]
        )
    scope = _operation_scope(flow, "PREPARE")
    return Plan(tuple(replace(operation, scope=scope) for operation in operations))


def build_cleanup_plan(
    flow: object, inventory: AwsInventorySnapshot, *, labels: tuple[str, ...]
) -> Plan:
    old_domain, new_domain = _pair(flow)
    _validate_common(old_domain, new_domain, inventory)
    old_zone = require_exact_zone(inventory.hosted_zones, old_domain)
    allowed_records = {(old_domain, "NS"), (old_domain, "SOA")}
    for label in labels:
        alias = f"{label}.{old_domain}"
        allowed_records.update({(alias, "A"), (alias, "AAAA")})
    foreign = [
        f"{record.name}:{record.type}"
        for record in old_zone.records
        if (record.name, record.type) not in allowed_records
    ]
    if foreign:
        raise UnsafePlan(f"old hosted zone contains foreign record: {foreign[0]}")

    operations: list[Operation] = []
    for label in labels:
        alias = f"{label}.{old_domain}"
        matches = [item for item in inventory.distributions if alias in item.aliases]
        if len(matches) > 1:
            raise UnsafePlan(f"expected at most one distribution for alias {alias}")
        if matches:
            operations.append(
                Operation(
                    "REMOVE_CLOUDFRONT_ALIAS",
                    f"{matches[0].id}:{alias}",
                    {"distribution_id": matches[0].id, "alias": alias},
                    (f"protect-new-domain:{new_domain}",),
                )
            )

    for label in labels:
        alias = f"{label}.{old_domain}"
        for record_type in ("A", "AAAA"):
            if any(
                record.name == alias and record.type == record_type for record in old_zone.records
            ):
                operations.append(
                    Operation(
                        "DELETE_DNS_RECORD",
                        f"{old_zone.id}:{alias}:{record_type}",
                        {"hosted_zone_id": old_zone.id, "name": alias, "type": record_type},
                    )
                )
    operations.append(
        Operation(
            "DELETE_HOSTED_ZONE",
            old_zone.id,
            {"hosted_zone_id": old_zone.id, "apex": old_domain},
            ("only-ns-soa-remain",),
        )
    )

    skipped: list[str] = []
    for certificate in inventory.certificates:
        if not certificate.sans or not all(
            _belongs_to(domain, old_domain) for domain in certificate.sans
        ):
            continue
        if certificate.in_use_by:
            skipped.append(f"certificate {certificate.arn} is still attached")
            continue
        operations.append(
            Operation(
                "DELETE_UNUSED_CERTIFICATE",
                certificate.arn,
                {"certificate_arn": certificate.arn},
                ("in-use-by-empty", f"protect-new-domain:{new_domain}"),
            )
        )

    registrations = [item for item in inventory.registered_domains if item.name == old_domain]
    if len(registrations) != 1:
        raise UnsafePlan("expected exactly one registered old domain")
    if registrations[0].auto_renew:
        operations.append(
            Operation(
                "DISABLE_AUTO_RENEW",
                old_domain,
                {"domain_name": old_domain},
                (f"exact-old-domain:{old_domain}",),
            )
        )
    _assert_no_new_domain_target(operations, new_domain)
    scope = _operation_scope(flow, "CLEANUP")
    return Plan(
        tuple(replace(operation, scope=scope) for operation in operations),
        tuple(skipped),
    )


def _pair(flow: object) -> tuple[str, str]:
    if hasattr(flow, "old_domain") and hasattr(flow, "new_domain"):
        return _domain(flow.old_domain), _domain(flow.new_domain)
    current = _domain(flow.current_domain)
    if not flow.spare_domains:
        raise UnsafePlan("new domain is missing")
    return current, _domain(flow.spare_domains[0])


def _operation_scope(flow: object, phase: str) -> str:
    flow_id = getattr(flow, "id", "unpersisted")
    return f"{flow_id}:{phase}"


def _validate_common(old_domain: str, new_domain: str, inventory: AwsInventorySnapshot) -> None:
    require_complete(inventory.complete)
    require_distinct(old_domain, new_domain)


def _exact_alias_distribution(inventory: AwsInventorySnapshot, alias: str) -> Distribution:
    matches = [item for item in inventory.distributions if alias in item.aliases]
    if len(matches) != 1:
        raise UnsafePlan(f"expected exactly one distribution for old alias {alias}")
    return matches[0]


def _assert_no_new_domain_target(operations: list[Operation], new_domain: str) -> None:
    if any(_belongs_to_target(operation.target, new_domain) for operation in operations):
        raise UnsafePlan("cleanup plan targets the new domain")


def _belongs_to_target(target: str, domain: str) -> bool:
    return domain in {part.lower().rstrip(".") for part in target.replace(":", " ").split()}


def _belongs_to(name: str, apex: str) -> bool:
    return name == apex or name.endswith(f".{apex}")


def _domain(value: str) -> str:
    return value.strip().rstrip(".").lower()
