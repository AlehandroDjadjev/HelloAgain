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
