from __future__ import annotations

from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase

from apps.agent_plans.services.intent_service import IntentResult
from apps.agent_sessions.models import SessionStatus
from apps.agent_sessions.services import SessionService
from apps.agent_sessions.views import _get_session


class SessionViewHelpersTests(TestCase):
    def test_get_session_retries_after_sqlite_lock(self):
        session = SessionService.create(
            user_id="view-test",
            device_id="device-1",
            input_mode="text",
            supported_packages=["com.android.chrome"],
        )

        with patch(
            "apps.agent_sessions.views.AgentSession.objects.get",
            side_effect=[
                OperationalError("database is locked"),
                session,
            ],
        ) as mocked_get:
            resolved = _get_session(session.id)

        self.assertEqual(resolved.id, session.id)
        self.assertEqual(mocked_get.call_count, 2)


class AgentCommandViewTests(TestCase):
    @patch("apps.agent_sessions.views.IntentService.parse_intent")
    def test_command_endpoint_creates_and_prepares_session(self, mock_parse_intent):
        mock_parse_intent.return_value = IntentResult(
            goal="Navigate to Central Park",
            goal_type="navigate_to",
            app_package="com.google.android.apps.maps",
            target_app="Google Maps",
            entities={"destination": "Central Park"},
            risk_level="medium",
            confidence=0.92,
            ambiguity_flags=[],
        )

        response = self.client.post(
            "/api/agent/command/",
            data={
                "prompt": "Take me to Central Park",
                "device_id": "pixel-1",
                "input_mode": "text",
                "reasoning_provider": "local",
                "supported_packages": ["com.google.android.apps.maps"],
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["session_status"], SessionStatus.EXECUTING)
        self.assertTrue(payload["execution_ready"])
        self.assertEqual(payload["intent"]["goal_type"], "navigate_to")
        self.assertEqual(
            payload["intent"]["app_package"],
            "com.google.android.apps.maps",
        )

        session = SessionService.get(payload["session_id"])
        self.assertEqual(session.device_id, "pixel-1")
        self.assertEqual(session.transcript, "Take me to Central Park")
        self.assertEqual(session.status, SessionStatus.EXECUTING)
        self.assertEqual(session.goal, "Navigate to Central Park")

    @patch("apps.agent_sessions.views.IntentService.parse_intent")
    def test_phone_command_endpoint_creates_and_prepares_session(
        self,
        mock_parse_intent,
    ):
        mock_parse_intent.return_value = IntentResult(
            goal="Open Chrome",
            goal_type="open_app",
            app_package="com.android.chrome",
            target_app="Chrome",
            entities={},
            risk_level="low",
            confidence=0.97,
            ambiguity_flags=[],
        )

        response = self.client.post(
            "/api/agent/phone-command/",
            data={
                "prompt": "Open Chrome",
                "device_id": "pixel-1",
                "input_mode": "text",
                "reasoning_provider": "openai",
                "supported_packages": ["com.android.chrome"],
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["session_status"], SessionStatus.EXECUTING)
        self.assertTrue(payload["execution_ready"])
        self.assertEqual(payload["intent"]["goal_type"], "open_app")
        self.assertEqual(payload["intent"]["app_package"], "com.android.chrome")

        session = SessionService.get(payload["session_id"])
        self.assertEqual(session.device_id, "pixel-1")
        self.assertEqual(session.transcript, "Open Chrome")
        self.assertEqual(session.reasoning_provider, "openai")
        self.assertEqual(session.status, SessionStatus.EXECUTING)

    def test_navigation_prepare_endpoint_uses_deterministic_maps_flow(self):
        response = self.client.post(
            "/api/agent/navigation/prepare/",
            data={
                "prompt": "Take me to Central Park",
                "device_id": "pixel-1",
                "supported_packages": ["com.google.android.apps.maps"],
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["session_status"], SessionStatus.APPROVED)
        self.assertTrue(payload["execution_ready"])
        self.assertEqual(payload["intent"]["goal_type"], "navigate_to")
        self.assertEqual(
            payload["intent"]["app_package"],
            "com.google.android.apps.maps",
        )
        self.assertEqual(
            payload["intent"]["entities"]["destination"],
            "central park",
        )
        self.assertGreater(payload["debug"]["step_count"], 0)

        session = SessionService.get(payload["session_id"])
        self.assertEqual(session.reasoning_provider, "openai")


class SessionUserInputViewTests(TestCase):
    def _make_session_with_pending_query(self):
        session = SessionService.create(
            user_id="clarification-test",
            device_id="device-1",
            transcript="Message Alex on WhatsApp",
            input_mode="text",
            supported_packages=["com.whatsapp"],
        )
        session.store_intent_data(
            goal="Message Alex on WhatsApp",
            target_app="com.whatsapp",
            entities={},
        )
        session.status = SessionStatus.AWAITING_USER_INPUT
        session.pending_user_input = {
            "query_id": "query_abc",
            "status": "pending",
            "question": "Which Alex should I message? I see Alex Chen and Alex Johnson.",
            "followup_question": "Which Alex should I message? I see Alex Chen and Alex Johnson.",
            "attempt_count": 1,
            "max_attempts": 3,
            "required_fields": ["recipient"],
            "candidates": ["Alex Chen", "Alex Johnson"],
            "ui_context": {"candidates": ["Alex Chen", "Alex Johnson"]},
            "reason": "multiple_visible_matches",
            "last_user_reply": "",
            "why_unresolved": "",
            "fallback_mode": "",
            "entity_updates": {},
        }
        session.save(update_fields=["status", "pending_user_input", "updated_at"])
        return session.id

    def test_user_input_endpoint_resolves_and_merges_entities(self):
        session_id = self._make_session_with_pending_query()

        response = self.client.post(
            f"/api/agent/sessions/{session_id}/user-input/",
            data={
                "query_id": "query_abc",
                "transcript": "Alex Johnson",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "resolved")
        self.assertTrue(payload["resolved"])
        self.assertEqual(payload["entity_updates"]["recipient"], "Alex Johnson")
        self.assertEqual(payload["resolved_values"]["recipient"], "Alex Johnson")
        self.assertEqual(payload["missing_fields"], [])
        self.assertEqual(payload["matched_candidate_id"], "candidate_2")
        self.assertFalse(payload["should_fallback"])

        session = SessionService.get(session_id)
        self.assertEqual(session.status, SessionStatus.EXECUTING)
        self.assertEqual(session.entities["recipient"], "Alex Johnson")
        self.assertEqual(session.pending_user_input, {})

    def test_user_input_endpoint_returns_409_for_stale_query_id(self):
        session_id = self._make_session_with_pending_query()

        response = self.client.post(
            f"/api/agent/sessions/{session_id}/user-input/",
            data={
                "query_id": "wrong_query",
                "transcript": "Alex Johnson",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)

    def test_user_input_endpoint_returns_409_when_session_is_not_awaiting_input(self):
        session_id = self._make_session_with_pending_query()
        session = SessionService.get(session_id)
        session.status = SessionStatus.EXECUTING
        session.save(update_fields=["status", "updated_at"])

        response = self.client.post(
            f"/api/agent/sessions/{session_id}/user-input/",
            data={
                "query_id": "query_abc",
                "transcript": "Alex Johnson",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)

    def test_user_input_endpoint_returns_retryable_followup(self):
        session_id = self._make_session_with_pending_query()

        response = self.client.post(
            f"/api/agent/sessions/{session_id}/user-input/",
            data={
                "query_id": "query_abc",
                "transcript": "Alex",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "needs_user_input")
        self.assertFalse(payload["resolved"])
        self.assertEqual(payload["attempt"], 2)
        self.assertEqual(payload["missing_fields"], ["recipient"])
        self.assertEqual(payload["reason_unresolved"], "multiple_matches")
        self.assertFalse(payload["should_fallback"])
        self.assertIn("Please choose exactly one", payload["followup_question"])

        session = SessionService.get(session_id)
        self.assertEqual(session.status, SessionStatus.AWAITING_USER_INPUT)
        self.assertEqual(session.pending_user_input["attempt_count"], 2)
        self.assertEqual(session.pending_user_input["followup_question"], payload["followup_question"])

    def test_user_input_endpoint_falls_back_after_final_unresolved_attempt(self):
        session_id = self._make_session_with_pending_query()
        session = SessionService.get(session_id)
        session.pending_user_input["attempt_count"] = 3
        session.save(update_fields=["pending_user_input", "updated_at"])

        response = self.client.post(
            f"/api/agent/sessions/{session_id}/user-input/",
            data={
                "query_id": "query_abc",
                "transcript": "[silence]",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "manual_takeover")
        self.assertFalse(payload["resolved"])
        self.assertEqual(payload["reason_unresolved"], "empty_reply")
        self.assertTrue(payload["should_fallback"])
        self.assertEqual(payload["missing_fields"], ["recipient"])

        session.refresh_from_db()
        self.assertEqual(session.pending_user_input["status"], "fallback")
        self.assertEqual(session.pending_user_input["reason_unresolved"], "empty_reply")
