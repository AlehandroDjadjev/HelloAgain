from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.agent_sessions.clarification_service import ClarificationService
from apps.agent_sessions.models import SessionStatus
from apps.agent_sessions.services import SessionService


class ClarificationServiceTests(TestCase):
    def _make_session(self, *, entities: dict | None = None):
        session = SessionService.create(
            user_id="clarification-service-test",
            device_id="device-1",
            transcript="Message Alex on WhatsApp",
            input_mode="text",
            reasoning_provider="openai",
            supported_packages=["com.whatsapp"],
        )
        session.store_intent_data(
            goal="Message Alex on WhatsApp",
            target_app="com.whatsapp",
            entities=entities or {},
        )
        return session

    def _make_pending_query(self, session, *, attempt_count: int = 1):
        session.status = SessionStatus.AWAITING_USER_INPUT
        session.pending_user_input = {
            "query_id": "query_abc",
            "status": "pending" if attempt_count == 1 else "retryable",
            "question": "Which Alex should I message?",
            "followup_question": "Which Alex should I message?",
            "attempt_count": attempt_count,
            "max_attempts": 3,
            "required_fields": ["recipient"],
            "candidates": ["Alex Chen", "Alex Johnson"],
            "reason": "multiple_visible_matches",
            "ui_context": {"candidates": ["Alex Chen", "Alex Johnson"]},
        }
        session.save(update_fields=["status", "pending_user_input", "updated_at"])
        return dict(session.pending_user_input)

    def test_reflect_reply_resolves_visible_candidate_deterministically(self):
        session = self._make_session()
        pending = self._make_pending_query(session)

        result = ClarificationService.reflect_reply(
            session=session,
            pending_query=pending,
            transcript="Alex Johnson",
        )

        self.assertTrue(result["resolved"])
        self.assertEqual(result["missing_fields"], [])
        self.assertEqual(result["resolved_values"], {"recipient": "Alex Johnson"})
        self.assertEqual(result["matched_candidate_id"], "candidate_2")
        self.assertFalse(result["should_fallback"])

    def test_reflect_reply_returns_followup_for_ambiguous_reply(self):
        session = self._make_session()
        pending = self._make_pending_query(session)

        result = ClarificationService.reflect_reply(
            session=session,
            pending_query=pending,
            transcript="Alex",
        )

        self.assertFalse(result["resolved"])
        self.assertEqual(result["missing_fields"], ["recipient"])
        self.assertEqual(result["reason_unresolved"], "multiple_matches")
        self.assertIn("Please choose exactly one", result["followup_question"])
        self.assertFalse(result["should_fallback"])

    def test_reflect_reply_detects_conflicting_context(self):
        session = self._make_session(entities={"recipient": "Alex Chen"})
        pending = self._make_pending_query(session)

        result = ClarificationService.reflect_reply(
            session=session,
            pending_query=pending,
            transcript="Alex Johnson",
        )

        self.assertFalse(result["resolved"])
        self.assertEqual(result["reason_unresolved"], "conflicting_context")
        self.assertEqual(result["resolved_values"], {"recipient": "Alex Johnson"})
        self.assertEqual(result["matched_candidate_id"], "candidate_2")

    @patch("apps.agent_sessions.clarification_service.LLMClient.from_reasoning_provider")
    def test_reflect_reply_uses_model_only_when_deterministic_match_is_insufficient(
        self,
        mock_from_reasoning_provider,
    ):
        mock_client = MagicMock()
        mock_client.generate.return_value = {
            "resolved": True,
            "resolved_values": {"recipient": "Alex Johnson"},
            "matched_candidate_id": "candidate_2",
            "missing_fields": [],
            "reason_unresolved": "",
            "followup_question": "",
            "should_fallback": False,
        }
        mock_from_reasoning_provider.return_value = mock_client

        session = self._make_session()
        pending = self._make_pending_query(session)

        result = ClarificationService.reflect_reply(
            session=session,
            pending_query=pending,
            transcript="the coworker from work",
        )

        self.assertTrue(result["resolved"])
        self.assertEqual(result["resolved_values"]["recipient"], "Alex Johnson")
        self.assertEqual(result["matched_candidate_id"], "candidate_2")
        mock_client.generate.assert_called_once()

    def test_submit_reply_falls_back_on_third_unresolved_attempt(self):
        session = self._make_session()
        self._make_pending_query(session, attempt_count=3)

        result = ClarificationService.submit_reply(
            session=session,
            query_id="query_abc",
            transcript="[silence]",
        )

        self.assertEqual(result["status"], "manual_takeover")
        self.assertEqual(result["reason_unresolved"], "empty_reply")
        self.assertTrue(result["should_fallback"])

        session.refresh_from_db()
        self.assertEqual(session.pending_user_input["status"], "fallback")
        self.assertEqual(session.pending_user_input["reason_unresolved"], "empty_reply")
