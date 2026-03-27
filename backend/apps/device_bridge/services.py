"""
DeviceBridgeService — persists screen states and action events,
advances session state on each result, and triggers audit records.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from django.core.cache import cache
from django.db import transaction

from apps.agent_core.enums import ActionResultStatus
from apps.agent_sessions.models import AgentSession, SessionStatus
from apps.agent_sessions.services import SessionService
from apps.audit_log.services import AuditService
from apps.audit_log.models import AuditActor, AuditEventType

from .models import AgentActionEvent, DeviceScreenState

logger = logging.getLogger(__name__)


class DeviceBridgeService:
    @staticmethod
    @transaction.atomic
    def ingest_screen_state(
        session: AgentSession,
        step_id: str,
        foreground_package: str,
        window_title: str,
        screen_hash: str,
        is_sensitive: bool,
        nodes: list,
        captured_at: datetime,
        focused_element_ref: str = "",
    ) -> DeviceScreenState:
        # Android payloads may legitimately send null for optional text fields.
        # Normalize them here so SQLite never sees NULL for non-null CharFields.
        state = DeviceScreenState.objects.create(
            session=session,
            step_id=_coerce_text(step_id),
            foreground_package=_coerce_text(foreground_package),
            window_title=_coerce_text(window_title),
            screen_hash=_coerce_text(screen_hash),
            focused_element_ref=_coerce_text(focused_element_ref),
            is_sensitive=is_sensitive,
            nodes=nodes or [],
            captured_at=captured_at,
        )
        if is_sensitive:
            AuditService.record(
                session=session,
                event_type=AuditEventType.SENSITIVE_SCREEN_DETECTED,
                actor=AuditActor.ANDROID,
                payload={
                    "screen_hash": screen_hash,
                    "foreground_package": foreground_package,
                    "step_id": step_id,
                },
            )
            logger.warning(
                "Sensitive screen detected: session=%s pkg=%s",
                session.id, foreground_package,
            )
        return state

    @staticmethod
    @transaction.atomic
    def record_action_result(
        session: AgentSession,
        plan_id: UUID,
        step_id: str,
        step_type: str,
        status: str,
        executed_at: datetime,
        error_code: str = "",
        error_detail: str = "",
        screen_state: Optional[DeviceScreenState] = None,
        duration_ms: int = 0,
    ) -> AgentActionEvent:
        event = AgentActionEvent.objects.create(
            session=session,
            plan_id=plan_id,
            step_id=step_id,
            step_type=step_type,
            status=status,
            error_code=error_code,
            error_detail=error_detail,
            screen_state=screen_state,
            duration_ms=duration_ms,
            executed_at=executed_at,
        )

        if status == ActionResultStatus.SUCCESS.value:
            SessionService.advance_step(session)
            AuditService.record(
                session=session,
                event_type=AuditEventType.STEP_SUCCEEDED,
                actor=AuditActor.ANDROID,
                payload={
                    "step_id": step_id,
                    "step_type": step_type,
                    "duration_ms": duration_ms,
                },
            )
        else:
            AuditService.record(
                session=session,
                event_type=AuditEventType.STEP_FAILED,
                actor=AuditActor.ANDROID,
                payload={
                    "step_id": step_id,
                    "step_type": step_type,
                    "status": status,
                    "error_code": error_code,
                    "error_detail": error_detail,
                },
            )
            # Session lifecycle decisions (abort, retry, continue) belong
            # exclusively to ExecutionService.decide_after_result, which runs
            # after this call and applies nuanced LLM-mode / plan-mode logic.
            # record_action_result must not kill the session here.

        return event


_SCREENSHOT_TTL = 30  # seconds — long enough for the next /next-step/ call


def store_screenshot(session_id: str, screenshot_b64: str) -> None:
    """Cache a failure screenshot keyed by session; expires after 30 s."""
    cache.set(f"screenshot:{session_id}", screenshot_b64, timeout=_SCREENSHOT_TTL)


def pop_screenshot(session_id: str) -> Optional[str]:
    """Consume and return the cached screenshot, or None if absent/expired."""
    val = cache.get(f"screenshot:{session_id}")
    if val:
        cache.delete(f"screenshot:{session_id}")
    return val


def _coerce_text(value: object) -> str:
    if value is None:
        return ""
    return str(value)
