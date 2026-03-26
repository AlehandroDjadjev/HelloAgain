"""
Celery tasks for plan compilation.

compile_plan_task is queued when a session receives an intent.
It will call the LLM planner (Stage 5) and store the resulting ActionPlan.
Until then it logs a stub message and transitions the session to plan_ready
with a placeholder plan for testing.
"""
import logging
from uuid import UUID

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=5)
def compile_plan_task(self, session_id: str) -> None:
    """
    Async task: parse intent → compile typed ActionPlan → persist.
    Triggered after POST /api/agent/sessions/{id}/intent/.
    LLM integration added in Stage 5.
    """
    logger.info("[STUB] compile_plan_task called for session_id=%s", session_id)
    # Stage 5: call LLM planner, validate, call PlanService.store_plan()
