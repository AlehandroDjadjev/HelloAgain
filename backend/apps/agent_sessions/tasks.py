"""
Celery tasks for session orchestration.

execute_next_step_task dispatches the next pending step to Android
via the device bridge. Implemented in Stage 6 (fixed JSON plan runner).
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=2, default_retry_delay=2)
def execute_next_step_task(self, session_id: str) -> None:
    """
    Async task: fetch next step from plan → dispatch to Android device.
    Implemented in Stage 6.
    """
    logger.info("[STUB] execute_next_step_task called for session_id=%s", session_id)
