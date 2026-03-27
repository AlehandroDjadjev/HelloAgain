"""
ConfirmationService — owns the full lifecycle of ConfirmationRecord objects.

Responsibilities:
  create()  — persist a new confirmation request, gate the session
  resolve() — approve or reject, resume or abort the session accordingly
  get_pending() — fetch the single pending record for a session (or None)

SessionService.create_confirmation / SessionService.resolve_confirmation
are kept as thin shims that forward here for backward compatibility.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from django.db import transaction

from apps.audit_log.models import AuditActor, AuditEventType
from apps.audit_log.services import AuditService

from .models import AgentSession, ConfirmationRecord, SessionStatus

logger = logging.getLogger(__name__)


class ConfirmationService:

    @staticmethod
    @transaction.atomic
    def create(
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
        """
        Persist a confirmation request and transition the session to
        AWAITING_CONFIRMATION.

        Idempotent within a single step: if a record already exists for
        (session, step_id) it is returned without creating a duplicate.
        """
        # Idempotency guard — ExecutionService may call this on retry
        existing = ConfirmationRecord.objects.filter(
            session=session, step_id=step_id
        ).first()
        if existing:
            return existing

        conf = ConfirmationRecord.objects.create(
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

        # Import here to avoid circular dependency
        from .services import SessionService
        SessionService.transition(session, SessionStatus.AWAITING_CONFIRMATION)

        AuditService.record(
            session=session,
            event_type=AuditEventType.CONFIRMATION_REQUESTED,
            actor=AuditActor.SYSTEM,
            payload={
                "confirmation_id": str(conf.id),
                "step_id": step_id,
                "app_package": app_package,
                "action_summary": action_summary,
            },
        )
        logger.info(
            "Confirmation created: id=%s step=%s session=%s",
            conf.id, step_id, session.id,
        )
        return conf

    @staticmethod
    @transaction.atomic
    def resolve(
        confirmation_id: UUID,
        approved: bool,
        session: AgentSession,
    ) -> ConfirmationRecord:
        """
        Approve or reject a pending confirmation.

        Approved  → session transitions to EXECUTING.
        Rejected  → session transitions to ABORTED.

        Raises ValueError if the record is not in PENDING state (idempotency
        caller should catch and return 409 Conflict).
        """
        conf = ConfirmationRecord.objects.select_for_update().get(pk=confirmation_id)

        if conf.status != ConfirmationRecord.Status.PENDING:
            raise ValueError(
                f"Confirmation {confirmation_id} is already '{conf.status}'."
            )

        conf.status = (
            ConfirmationRecord.Status.APPROVED
            if approved
            else ConfirmationRecord.Status.REJECTED
        )
        conf.resolved_at = datetime.now(timezone.utc)
        conf.save(update_fields=["status", "resolved_at"])

        from .services import SessionService

        event_type = (
            AuditEventType.CONFIRMATION_APPROVED
            if approved
            else AuditEventType.CONFIRMATION_REJECTED
        )
        AuditService.record(
            session=session,
            event_type=event_type,
            actor=AuditActor.USER,
            payload={"confirmation_id": str(confirmation_id)},
        )

        if approved:
            SessionService.transition(session, SessionStatus.EXECUTING)
            logger.info(
                "Confirmation %s approved → session %s EXECUTING",
                confirmation_id, session.id,
            )
        else:
            SessionService.transition(session, SessionStatus.ABORTED)
            AuditService.record(
                session=session,
                event_type=AuditEventType.SESSION_ABORTED,
                actor=AuditActor.USER,
                payload={"reason": "confirmation_rejected"},
            )
            logger.info(
                "Confirmation %s rejected → session %s ABORTED",
                confirmation_id, session.id,
            )

        return conf

    @staticmethod
    def get_pending(session: AgentSession) -> Optional[ConfirmationRecord]:
        """Return the first PENDING confirmation for a session, or None."""
        return ConfirmationRecord.objects.filter(
            session=session,
            status=ConfirmationRecord.Status.PENDING,
        ).first()

    @staticmethod
    def get_by_id(confirmation_id: UUID) -> ConfirmationRecord:
        """Fetch a record by PK; raises ConfirmationRecord.DoesNotExist if absent."""
        return ConfirmationRecord.objects.select_related("session").get(
            pk=confirmation_id
        )
