from __future__ import annotations

from datetime import datetime

from .models import Flow, FlowStatus
from .repository import Repository


class InvalidTransition(RuntimeError):
    pass


class DomainConfirmationError(ValueError):
    pass


class CommandService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def confirm_preparation(self, flow_id: str, actor: str, now: datetime) -> Flow:
        return self.repository.transition_and_enqueue(
            flow_id,
            expected=FlowStatus.AWAITING_PREPARATION_CONFIRMATION,
            queued=FlowStatus.PREPARATION_QUEUED,
            job_kind="PREPARE",
            actor=actor,
            now=now,
            action="PREPARATION_CONFIRMED",
        )

    def confirm_backoffice_switch(self, flow_id: str, actor: str, now: datetime) -> Flow:
        return self.repository.transition_and_enqueue(
            flow_id,
            expected=FlowStatus.AWAITING_BACKOFFICE_CONFIRMATION,
            queued=FlowStatus.CLEANUP_PLAN_QUEUED,
            job_kind="BUILD_CLEANUP_PLAN",
            actor=actor,
            now=now,
            action="BACKOFFICE_SWITCH_CONFIRMED",
        )

    def confirm_cleanup(
        self,
        flow_id: str,
        confirmed_old_domain: str,
        actor: str,
        now: datetime,
    ) -> Flow:
        flow = self.repository.get_flow(flow_id)
        if confirmed_old_domain.strip().lower().rstrip(".") != flow.old_domain:
            raise DomainConfirmationError("exact old domain confirmation is required")
        return self.repository.transition_and_enqueue(
            flow_id,
            expected=FlowStatus.AWAITING_CLEANUP_CONFIRMATION,
            queued=FlowStatus.CLEANUP_QUEUED,
            job_kind="CLEANUP",
            actor=actor,
            now=now,
            action="CLEANUP_CONFIRMED",
        )
