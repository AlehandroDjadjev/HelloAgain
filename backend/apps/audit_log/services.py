"""
AuditService — append-only audit trail writer.
Always called inside an outer transaction.atomic() by the owning service.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import OperationalError, transaction

from .models import AuditActor, AuditEventType, AuditRecord

if TYPE_CHECKING:
    from apps.agent_sessions.models import AgentSession

logger = logging.getLogger(__name__)


class AuditService:
    @staticmethod
    def record(
        session: "AgentSession",
        event_type: str,
        actor: str,
        payload: dict,
    ) -> AuditRecord | None:
        try:
            with transaction.atomic():
                record = AuditRecord(
                    session=session,
                    event_type=event_type,
                    actor=actor,
                    payload=payload,
                )
                record.save()
        except OperationalError as exc:
            logger.warning(
                "Audit write skipped for session=%s event=%s actor=%s: %s",
                session.id,
                event_type,
                actor,
                exc,
            )
            return None

        logger.debug("Audit: session=%s event=%s actor=%s", session.id, event_type, actor)
        return record
