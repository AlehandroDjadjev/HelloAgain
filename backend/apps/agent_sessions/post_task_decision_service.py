from __future__ import annotations

import json
import logging
from typing import Any

from apps.agent_core.llm_client import LLMClient, LLMError

from .models import AgentSession


logger = logging.getLogger(__name__)


class PostTaskDecisionService:
    """Decides how the post-task phone loop should proceed."""

    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client or LLMClient.from_reasoning_provider("openai")

    def decide(
        self,
        *,
        session: AgentSession,
        transcript: str,
        phase: str,
        current_app_package: str = "",
        current_app_name: str = "",
        current_window_title: str = "",
        last_assistant_message: str = "",
    ) -> dict[str, Any]:
        prompt_context = {
            "phase": self._normalize_phase(phase),
            "transcript": str(transcript or "").strip(),
            "last_assistant_message": str(last_assistant_message or "").strip(),
            "session_goal": str(session.goal or "").strip(),
            "target_app": str(session.target_app or "").strip(),
            "entities": session.entities or {},
            "session_status": str(session.status or "").strip(),
            "recent_steps": [
                {
                    "action_type": str(item.get("action_type") or "").strip(),
                    "reasoning": str(item.get("reasoning") or "").strip(),
                    "result_code": str(item.get("result_code") or "").strip(),
                    "result_success": bool(item.get("result_success")),
                }
                for item in session.get_recent_steps(6)
            ],
            "current_app_package": str(current_app_package or "").strip(),
            "current_app_name": str(current_app_name or "").strip(),
            "current_window_title": str(current_window_title or "").strip(),
        }

        try:
            result = self._client.generate(
                system_prompt=self._system_prompt(),
                user_prompt=json.dumps(prompt_context, ensure_ascii=False),
                json_mode=True,
            )
            return self._normalize_result(result, prompt_context)
        except LLMError as exc:
            logger.warning(
                "PostTaskDecisionService falling back after OpenAI failure for session=%s: %s",
                session.id,
                exc,
            )
            return {
                "decision": "ask_for_clarification",
                "reply_message": (
                    "Не успях да обработя отговора ви. Кажете отново дали да "
                    "продължим на телефона или да се върнем в приложението."
                ),
                "next_instruction": "",
            }

    def _normalize_result(
        self,
        result: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        raw_decision = str(result.get("decision") or "").strip().lower()
        if raw_decision not in {
            "return_to_app",
            "continue_session",
            "ask_for_clarification",
        }:
            raw_decision = "ask_for_clarification"

        reply_message = " ".join(str(result.get("reply_message") or "").split()).strip()
        next_instruction = " ".join(
            str(result.get("next_instruction") or "").split()
        ).strip()

        if raw_decision == "continue_session" and not next_instruction:
            next_instruction = str(context.get("transcript") or "").strip()

        if not reply_message and raw_decision == "ask_for_clarification":
            reply_message = (
                "Не разбрах дали искате да продължим на телефона или да се "
                "върнем в приложението. Кажете го още веднъж."
            )

        return {
            "decision": raw_decision,
            "reply_message": reply_message,
            "next_instruction": next_instruction,
        }

    @staticmethod
    def _normalize_phase(phase: str) -> str:
        lowered = str(phase or "").strip().lower()
        if lowered == "aborted":
            return "cancelled"
        if lowered in {"completed", "failed", "cancelled"}:
            return lowered
        return "completed"

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You decide what should happen after a phone-automation task has already ended. "
            "Return ONLY JSON.\n"
            "Write the reply_message in Bulgarian unless the user's transcript is clearly in another language.\n"
            "The task is already finished, failed, or cancelled. Your only job is to interpret the user's next utterance.\n"
            "The utterance may respond to the previous assistant message, so use last_assistant_message as context.\n"
            "Choose exactly one decision:\n"
            "- return_to_app: the user means they are done here and want to return to the main app page.\n"
            "- continue_session: the user is giving a new phone instruction and wants to keep working on the phone.\n"
            "- ask_for_clarification: the utterance is too unclear to decide.\n"
            "Do not rely on exact keywords only. Infer intent semantically.\n"
            "If the user means 'we are done', 'take me back', 'back to the app', 'that's enough', or equivalent wording, choose return_to_app.\n"
            "If the user gives another phone task, choose continue_session and copy that instruction into next_instruction.\n"
            "If the user clearly wants to continue in the current app context, still choose continue_session.\n"
            "Schema:\n"
            "{\n"
            '  "decision": "return_to_app | continue_session | ask_for_clarification",\n'
            '  "reply_message": "short assistant reply",\n'
            '  "next_instruction": "empty unless decision is continue_session"\n'
            "}"
        )
