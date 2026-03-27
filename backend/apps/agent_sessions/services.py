"""
SessionService — owns AgentSession and ConfirmationRecord lifecycle.
Views are thin; all business logic lives here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from django.db import transaction

from apps.audit_log.services import AuditService
from apps.audit_log.models import AuditEventType, AuditActor

from .models import AgentSession, ConfirmationRecord, SessionStatus

logger = logging.getLogger(__name__)


class SessionService:
    # Statuses from which pause is permitted
    PAUSEABLE = frozenset([
        SessionStatus.EXECUTING,
        SessionStatus.AWAITING_CONFIRMATION,
        SessionStatus.APPROVED,
    ])
    # Statuses that are terminal — no transitions allowed
    TERMINAL = frozenset([
        SessionStatus.COMPLETED,
        SessionStatus.ABORTED,
        SessionStatus.FAILED,
    ])

    @staticmethod
    @transaction.atomic
    def create(
        user_id: str,
        device_id: str = "",
        transcript: str = "",
        input_mode: str = "voice",
        reasoning_provider: str = "local",
        supported_packages: list | None = None,
    ) -> AgentSession:
        session = AgentSession.objects.create(
            user_id=user_id,
            device_id=device_id,
            transcript=transcript,
            input_mode=input_mode,
            reasoning_provider=reasoning_provider,
            supported_packages=supported_packages or [],
            status=SessionStatus.CREATED,
        )
        AuditService.record(
            session=session,
            event_type=AuditEventType.SESSION_CREATED,
            actor=AuditActor.USER,
            payload={
                "user_id": user_id,
                "device_id": device_id,
                "input_mode": input_mode,
                "reasoning_provider": reasoning_provider,
            },
        )
        logger.info("AgentSession created: %s (user=%s)", session.id, user_id)
        return session

    @staticmethod
    def get(session_id: UUID) -> AgentSession:
        return AgentSession.objects.get(pk=session_id)

    @staticmethod
    @transaction.atomic
    def transition(session: AgentSession, new_status: str) -> AgentSession:
        old_status = session.status
        session.status = new_status
        session.save(update_fields=["status", "updated_at"])
        logger.debug(
            "Session %s: %s → %s", session.id, old_status, new_status
        )
        return session

    @staticmethod
    @transaction.atomic
    def advance_step(session: AgentSession) -> AgentSession:
        session.current_step_index += 1
        session.save(update_fields=["current_step_index", "updated_at"])
        return session

    @staticmethod
    def create_confirmation(
        session: AgentSession,
        plan_id: UUID,
        step_id: str,
        app_name: str,
        app_package: str,
        action_summary: str,
        sensitivity: str,
        recipient: str = "",
        content_preview: str = "",
        expires_at: Optional[datetime] = None,
    ) -> ConfirmationRecord:
        """Shim — delegates to ConfirmationService.create()."""
        from .confirmation_service import ConfirmationService
        return ConfirmationService.create(
            session=session,
            plan_id=plan_id,
            step_id=step_id,
            app_name=app_name,
            app_package=app_package,
            action_summary=action_summary,
            sensitivity=sensitivity,
            recipient=recipient,
            content_preview=content_preview,
            expires_at=expires_at,
        )

    @staticmethod
    @transaction.atomic
    def pause(session: AgentSession) -> AgentSession:
        if session.status not in SessionService.PAUSEABLE:
            raise ValueError(
                f"Session {session.id} cannot be paused from status '{session.status}'."
            )
        session.previous_status = session.status
        session.status = SessionStatus.PAUSED
        session.save(update_fields=["status", "previous_status", "updated_at"])
        AuditService.record(
            session=session,
            event_type=AuditEventType.SESSION_PAUSED,
            actor=AuditActor.USER,
            payload={"previous_status": session.previous_status},
        )
        return session

    @staticmethod
    @transaction.atomic
    def resume(session: AgentSession) -> AgentSession:
        if session.status != SessionStatus.PAUSED:
            raise ValueError(
                f"Session {session.id} cannot be resumed from status '{session.status}'."
            )
        restore = session.previous_status or SessionStatus.EXECUTING
        session.status = restore
        session.previous_status = ""
        session.save(update_fields=["status", "previous_status", "updated_at"])
        AuditService.record(
            session=session,
            event_type=AuditEventType.SESSION_RESUMED,
            actor=AuditActor.USER,
            payload={"resumed_to": restore},
        )
        return session

    @staticmethod
    @transaction.atomic
    def cancel(session: AgentSession) -> AgentSession:
        if session.status in SessionService.TERMINAL:
            raise ValueError(
                f"Session {session.id} is already terminal (status={session.status})."
            )
        session.status = SessionStatus.ABORTED
        session.save(update_fields=["status", "updated_at"])
        AuditService.record(
            session=session,
            event_type=AuditEventType.SESSION_CANCELLED,
            actor=AuditActor.USER,
            payload={"reason": "user_cancelled"},
        )
        return session

    @staticmethod
    @transaction.atomic
    def heartbeat(
        session: AgentSession,
        current_step: int,
        foreground_package: str,
    ) -> dict:
        """Update last-alive timestamp and return session liveness."""
        from datetime import datetime, timezone as tz
        if session.status in SessionService.TERMINAL:
            return {"alive": False, "status": session.status}
        session.last_heartbeat_at = datetime.now(tz.utc)
        session.save(update_fields=["last_heartbeat_at", "updated_at"])
        return {
            "alive": True,
            "status": session.status,
            "expected_step_index": session.current_step_index,
        }

    @staticmethod
    def resolve_confirmation(
        confirmation_id: UUID,
        approved: bool,
        session: AgentSession,
    ) -> ConfirmationRecord:
        """Shim — delegates to ConfirmationService.resolve()."""
        from .confirmation_service import ConfirmationService
        return ConfirmationService.resolve(
            confirmation_id=confirmation_id,
            approved=approved,
            session=session,
        )
