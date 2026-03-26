"""
PlanService — owns ActionPlanRecord persistence and lifecycle.
Validates incoming plans against the Pydantic ActionPlan schema before persisting.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from django.db import transaction

from apps.agent_core.schemas import ActionPlan as ActionPlanSchema
from apps.agent_sessions.models import AgentSession, SessionStatus
from apps.agent_sessions.services import SessionService
from apps.audit_log.services import AuditService
from apps.audit_log.models import AuditEventType, AuditActor

from ..models import ActionPlanRecord, IntentRecord, PlanStatus

logger = logging.getLogger(__name__)

# Maps app package names to executor identifiers
_APP_EXECUTOR_MAP = {
    "com.whatsapp": "whatsapp_send_message_v1",
    "com.google.android.apps.maps": "maps_navigate_v1",
    "com.android.chrome": "chrome_browse_v1",
    "com.google.android.gm": "gmail_compose_v1",
}


class PlanService:
    @staticmethod
    @transaction.atomic
    def store_intent(
        session: AgentSession,
        raw_transcript: str,
        parsed_intent: dict,
        llm_raw_response: str = "",
        goal_type: str = "",
        confidence: float = 1.0,
        ambiguity_flags: list | None = None,
    ) -> IntentRecord:
        intent, _ = IntentRecord.objects.update_or_create(
            session=session,
            defaults={
                "raw_transcript": raw_transcript,
                "parsed_intent": parsed_intent,
                "llm_raw_response": llm_raw_response,
                "goal_type": goal_type,
                "confidence": confidence,
                "ambiguity_flags": ambiguity_flags or [],
            },
        )
        SessionService.transition(session, SessionStatus.PLANNING)
        AuditService.record(
            session=session,
            event_type=AuditEventType.INTENT_STORED,
            actor=AuditActor.SYSTEM,
            payload={
                "goal_type": goal_type,
                "confidence": confidence,
                "target_app": parsed_intent.get("app_package", ""),
                "ambiguous": confidence < 0.5,
            },
        )
        return intent

    @staticmethod
    @transaction.atomic
    def store_plan(session: AgentSession, validated_plan: ActionPlanSchema) -> ActionPlanRecord:
        """
        Persist a validated ActionPlan. The Pydantic schema has already been
        validated — this layer only handles persistence.
        Overwrites an existing draft plan (idempotent for re-compilation).
        """
        existing = ActionPlanRecord.objects.filter(session=session).first()
        if existing and existing.status not in (PlanStatus.DRAFT, PlanStatus.PENDING_APPROVAL):
            raise ValueError(
                f"Session {session.id} already has an approved/executing plan "
                f"(status={existing.status}). Cannot overwrite."
            )
        if existing:
            existing.delete()

        record = ActionPlanRecord.objects.create(
            id=validated_plan.plan_id,
            session=session,
            goal=validated_plan.goal,
            app_package=validated_plan.app_package,
            steps=[step.model_dump() for step in validated_plan.steps],
            status=PlanStatus.PENDING_APPROVAL,
            version=validated_plan.version,
        )
        SessionService.transition(session, SessionStatus.PLAN_READY)
        AuditService.record(
            session=session,
            event_type=AuditEventType.PLAN_COMPILED,
            actor=AuditActor.SYSTEM,
            payload={
                "plan_id": str(record.id),
                "step_count": record.step_count,
                "app_package": record.app_package,
            },
        )
        logger.info("Plan stored: %s (%d steps)", record.id, record.step_count)
        return record

    @staticmethod
    @transaction.atomic
    def approve_plan(session: AgentSession) -> ActionPlanRecord:
        record = ActionPlanRecord.objects.select_for_update().get(session=session)
        if record.status != PlanStatus.PENDING_APPROVAL:
            raise ValueError(
                f"Plan {record.id} cannot be approved from status '{record.status}'."
            )
        record.status = PlanStatus.APPROVED
        record.approved_at = datetime.now(timezone.utc)
        record.save(update_fields=["status", "approved_at"])
        SessionService.transition(session, SessionStatus.APPROVED)
        AuditService.record(
            session=session,
            event_type=AuditEventType.PLAN_APPROVED,
            actor=AuditActor.USER,
            payload={"plan_id": str(record.id)},
        )
        return record

    @staticmethod
    def get_current_step(session: AgentSession) -> Optional[dict]:
        try:
            record = ActionPlanRecord.objects.get(session=session)
        except ActionPlanRecord.DoesNotExist:
            return None
        steps = record.steps or []
        idx = session.current_step_index
        if idx >= len(steps):
            return None
        return steps[idx]

    @staticmethod
    def get_plan(session: AgentSession) -> Optional[ActionPlanRecord]:
        return ActionPlanRecord.objects.filter(session=session).first()

    @staticmethod
    def get_executor_hint(app_package: str) -> str:
        return _APP_EXECUTOR_MAP.get(app_package, "generic_v1")
