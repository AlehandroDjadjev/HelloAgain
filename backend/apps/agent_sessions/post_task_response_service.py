from __future__ import annotations

import json
import logging
from typing import Any

from apps.agent_core.llm_client import LLMClient, LLMError

from .models import AgentSession


logger = logging.getLogger(__name__)


class PostTaskResponseService:
    """Generates a dynamic post-task message for the phone-command flow."""

    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client or LLMClient.from_reasoning_provider("openai")

    def build_response(
        self,
        *,
        session: AgentSession,
        phase: str,
        error_message: str = "",
        current_reasoning: str = "",
    ) -> dict[str, Any]:
        normalized_phase = self._normalize_phase(phase)
        prompt_context = self._build_prompt_context(
            session=session,
            phase=normalized_phase,
            error_message=error_message,
            current_reasoning=current_reasoning,
        )

        try:
            result = self._client.generate(
                system_prompt=self._system_prompt(),
                user_prompt=json.dumps(prompt_context, ensure_ascii=False),
                json_mode=True,
            )
            return self._normalize_result(result, prompt_context)
        except LLMError as exc:
            logger.warning(
                "PostTaskResponseService falling back after OpenAI failure for session=%s: %s",
                session.id,
                exc,
            )
            return self._fallback_response(prompt_context)

    def _build_prompt_context(
        self,
        *,
        session: AgentSession,
        phase: str,
        error_message: str,
        current_reasoning: str,
    ) -> dict[str, Any]:
        recent_steps = []
        for entry in session.get_recent_steps(6):
            recent_steps.append(
                {
                    "action_type": str(entry.get("action_type") or "").strip(),
                    "result_success": bool(entry.get("result_success")),
                    "result_code": str(entry.get("result_code") or "").strip(),
                    "reasoning": str(entry.get("reasoning") or "").strip(),
                }
            )

        explicit_error = str(error_message or "").strip()
        if not explicit_error:
            explicit_error = self._latest_step_failure(session)

        return {
            "phase": phase,
            "session_status": str(session.status or "").strip(),
            "goal": str(session.goal or "").strip(),
            "target_app": str(session.target_app or "").strip(),
            "entities": session.entities or {},
            "risk_level": str(session.risk_level or "").strip(),
            "transcript": str(session.transcript or "").strip(),
            "step_count": session.get_step_count(),
            "recent_steps": recent_steps,
            "error_message": explicit_error,
            "current_reasoning": str(current_reasoning or "").strip(),
        }

    def _normalize_result(
        self,
        result: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        phase = self._normalize_phase(result.get("phase") or context.get("phase"))
        message = " ".join(str(result.get("message") or "").split()).strip()
        status_line = " ".join(str(result.get("status_line") or "").split()).strip()

        if not message or not status_line:
            fallback = self._fallback_response(context)
            if not message:
                message = fallback["message"]
            if not status_line:
                status_line = fallback["status_line"]

        return {
            "phase": phase,
            "message": message,
            "status_line": status_line,
            "allow_follow_up": True,
            "allow_return_to_app": True,
        }

    def _fallback_response(self, context: dict[str, Any]) -> dict[str, Any]:
        phase = self._normalize_phase(context.get("phase"))
        status_prefix = {
            "completed": "Задачата приключи.",
            "failed": "Задачата приключи с проблем.",
            "cancelled": "Задачата беше прекратена.",
        }.get(phase, "Задачата приключи.")
        message = (
            f"{status_prefix} Можете да дадете нова инструкция за телефона "
            "или да поискате връщане към основното приложение."
        )
        return {
            "phase": phase,
            "message": message,
            "status_line": message,
            "allow_follow_up": True,
            "allow_return_to_app": True,
        }

    def _latest_step_failure(self, session: AgentSession) -> str:
        for entry in reversed(session.step_history or []):
            if entry.get("result_success") is False:
                result_code = str(entry.get("result_code") or "").strip()
                reasoning = str(entry.get("reasoning") or "").strip()
                if reasoning and result_code:
                    return f"{reasoning} ({result_code})"
                if reasoning:
                    return reasoning
                if result_code:
                    return result_code
        return ""

    def _normalize_phase(self, phase: object) -> str:
        lowered = str(phase or "").strip().lower()
        if lowered == "aborted":
            return "cancelled"
        if lowered in {"completed", "failed", "cancelled"}:
            return lowered
        return "completed"

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You generate the follow-up assistant message after a mobile phone-automation task. "
            "Return ONLY JSON.\n"
            "Write in Bulgarian unless the transcript is clearly in another language.\n"
            "Be specific about what succeeded or why it failed, based on the provided session data.\n"
            "Do not invent actions that did not happen.\n"
            "Always give the user both options in the message: "
            "1) they can give a new phone command, and "
            "2) they can ask to return to the main app.\n"
            "Keep the response concise, natural, and conversational.\n"
            "Schema:\n"
            "{\n"
            '  "phase": "completed | failed | cancelled",\n'
            '  "message": "1-3 short sentences, spoken reply for the user",\n'
            '  "status_line": "short UI summary, max 160 chars"\n'
            "}"
        )
