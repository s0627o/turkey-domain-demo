from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class FlowStatus(str, Enum):
    AWAITING_PREPARATION_CONFIRMATION = "AWAITING_PREPARATION_CONFIRMATION"
    PREPARATION_QUEUED = "PREPARATION_QUEUED"
    PREPARING = "PREPARING"
    AWAITING_BACKOFFICE_CONFIRMATION = "AWAITING_BACKOFFICE_CONFIRMATION"
    SWITCH_COMMIT_QUEUED = "SWITCH_COMMIT_QUEUED"
    COMMITTING_SWITCH = "COMMITTING_SWITCH"
    CLEANUP_PLAN_QUEUED = "CLEANUP_PLAN_QUEUED"
    PREPARING_CLEANUP_PLAN = "PREPARING_CLEANUP_PLAN"
    AWAITING_CLEANUP_CONFIRMATION = "AWAITING_CLEANUP_CONFIRMATION"
    CLEANUP_QUEUED = "CLEANUP_QUEUED"
    CLEANING = "CLEANING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_LOCKED = "FAILED_LOCKED"
    ADMIN_UNLOCKED = "ADMIN_UNLOCKED"


@dataclass(frozen=True)
class DomainInventory:
    current_domain: str
    spare_domains: tuple[str, ...]
    revision: str
    observed_at: datetime


@dataclass(frozen=True)
class Flow:
    id: str
    old_domain: str
    new_domain: str
    actor_email: str
    inventory_revision: str
    inventory_observed_at: datetime
    status: FlowStatus
    note: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class GlobalLock:
    flow_id: str
    acquired_at: datetime


@dataclass(frozen=True)
class StageEvent:
    stage: FlowStatus
    occurred_at: datetime
    summary: str


@dataclass(frozen=True)
class AuditEvent:
    actor: str
    action: str
    occurred_at: datetime
    detail_json: str


@dataclass(frozen=True)
class FlowDetail:
    flow: Flow
    stages: tuple[StageEvent, ...]
    audit: tuple[AuditEvent, ...]


@dataclass(frozen=True)
class Job:
    id: str
    flow_id: str
    kind: str
    status: str
    lease_owner: str | None
    lease_deadline: datetime | None
    attempts: int
