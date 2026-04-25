from __future__ import annotations

from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase

from apps.agent_core.llm_client import LLMError
from apps.agent_plans.services.intent_service import IntentResult
from apps.agent_sessions.models import SessionStatus
from apps.agent_sessions.post_task_decision_service import PostTaskDecisionService
from apps.agent_sessions.post_task_response_service import PostTaskResponseService
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

    @patch("apps.agent_sessions.views.IntentService.parse_intent")
    def test_phone_command_endpoint_keeps_vague_prompt_in_clarification_state(
        self,
        mock_parse_intent,
    ):
        mock_parse_intent.return_value = IntentResult(
            goal="Send a message",
            goal_type="send_message",
            app_package="com.whatsapp",
            target_app="WhatsApp",
            entities={},
            risk_level="high",
            confidence=0.72,
            ambiguity_flags=[],
            needs_clarification=True,
            missing_fields=["recipient", "message"],
            clarification_question="Who should I message, and what should I say?",
        )

        response = self.client.post(
            "/api/agent/phone-command/",
            data={
                "prompt": "Send a WhatsApp message",
                "device_id": "pixel-1",
                "input_mode": "text",
                "reasoning_provider": "openai",
                "supported_packages": ["com.whatsapp"],
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["session_status"], SessionStatus.PLANNING)
        self.assertFalse(payload["execution_ready"])
        self.assertTrue(payload["intent"]["needs_clarification"])
        self.assertEqual(
            payload["intent"]["clarification_question"],
            "Who should I message, and what should I say?",
        )

        session = SessionService.get(payload["session_id"])
        self.assertEqual(session.status, SessionStatus.PLANNING)
        self.assertEqual(session.goal, "")
        self.assertEqual(session.target_app, "")

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

    @patch("apps.agent_sessions.views.PostTaskResponseService.build_response")
    def test_terminal_response_endpoint_returns_generated_message(
        self,
        mock_build_response,
    ):
        session = SessionService.create(
            user_id="view-test",
            device_id="pixel-1",
            transcript="Open Chrome",
            input_mode="text",
            reasoning_provider="openai",
            supported_packages=["com.android.chrome"],
        )
        session.store_intent_data(
            goal="Open Chrome",
            target_app="com.android.chrome",
            entities={},
            risk_level="low",
        )

        mock_build_response.return_value = {
            "phase": "completed",
            "message": "Отворих Chrome. Можете да кажете нова команда или върни се.",
            "status_line": "Отворих Chrome. Кажете нова команда или върни се.",
            "allow_follow_up": True,
            "allow_return_to_app": True,
        }

        response = self.client.post(
            f"/api/agent/sessions/{session.id}/terminal-response/",
            data={
                "phase": "completed",
                "error_message": "",
                "current_reasoning": "Chrome is open.",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["phase"], "completed")
        self.assertIn("нова команда", payload["message"])
        self.assertTrue(payload["allow_follow_up"])
        self.assertTrue(payload["allow_return_to_app"])
        mock_build_response.assert_called_once()

    @patch("apps.agent_sessions.views.PostTaskDecisionService.decide")
    def test_post_task_decision_endpoint_returns_model_decision(
        self,
        mock_decide,
    ):
        session = SessionService.create(
            user_id="view-test",
            device_id="pixel-1",
            transcript="Open Chrome",
            input_mode="text",
            reasoning_provider="openai",
            supported_packages=["com.android.chrome"],
        )
        session.store_intent_data(
            goal="Open Chrome",
            target_app="com.android.chrome",
            entities={},
            risk_level="low",
        )

        mock_decide.return_value = {
            "decision": "return_to_app",
            "reply_message": "Разбрах. Връщам ви към основната страница на приложението.",
            "next_instruction": "",
        }

        response = self.client.post(
            f"/api/agent/sessions/{session.id}/post-task-decision/",
            data={
                "transcript": "Готово, върни ме в приложението",
                "phase": "completed",
                "current_app_package": "com.android.chrome",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["decision"], "return_to_app")
        self.assertIn("приложението", payload["reply_message"])
        mock_decide.assert_called_once()


class PostTaskResponseServiceTests(TestCase):
    def test_service_uses_openai_output_when_available(self):
        class FakeClient:
            def generate(self, **kwargs):
                return {
                    "phase": "failed",
                    "message": "Не успях да изпратя съобщението до Иван. Можете да дадете нова команда или да кажете върни се.",
                    "status_line": "Не успях да изпратя съобщението. Кажете нова команда или върни се.",
                }

        session = SessionService.create(
            user_id="service-test",
            device_id="pixel-1",
            transcript="Send Ivan a message",
            input_mode="text",
            reasoning_provider="openai",
            supported_packages=["com.viber.voip"],
        )
        session.store_intent_data(
            goal="Send Ivan a message",
            target_app="com.viber.voip",
            entities={"recipient": "Ivan"},
            risk_level="high",
        )

        payload = PostTaskResponseService(client=FakeClient()).build_response(
            session=session,
            phase="failed",
            error_message="Target chat was not found",
            current_reasoning="The user Ivan was not visible.",
        )

        self.assertEqual(payload["phase"], "failed")
        self.assertIn("Иван", payload["message"])
        self.assertTrue(payload["allow_return_to_app"])

    def test_service_falls_back_dynamically_when_openai_fails(self):
        class FailingClient:
            def generate(self, **kwargs):
                raise LLMError("quota exceeded")

        session = SessionService.create(
            user_id="service-test",
            device_id="pixel-1",
            transcript="Open Viber",
            input_mode="text",
            reasoning_provider="openai",
            supported_packages=["com.viber.voip"],
        )
        session.store_intent_data(
            goal="Open Viber",
            target_app="com.viber.voip",
            entities={},
            risk_level="low",
        )

        payload = PostTaskResponseService(client=FailingClient()).build_response(
            session=session,
            phase="completed",
        )

        self.assertEqual(payload["phase"], "completed")
        self.assertIn("нова инструкция", payload["message"])
        self.assertIn("основното приложение", payload["message"])


class PostTaskDecisionServiceTests(TestCase):
    def test_service_uses_openai_decision_when_available(self):
        class FakeClient:
            def generate(self, **kwargs):
                return {
                    "decision": "continue_session",
                    "reply_message": "Разбрах. Продължавам с новата задача на телефона.",
                    "next_instruction": "Превърти надолу и отвори първия резултат",
                }

        session = SessionService.create(
            user_id="service-test",
            device_id="pixel-1",
            transcript="Search in Chrome",
            input_mode="text",
            reasoning_provider="openai",
            supported_packages=["com.android.chrome"],
        )
        session.store_intent_data(
            goal="Search in Chrome",
            target_app="com.android.chrome",
            entities={"query": "weather"},
            risk_level="low",
        )

        payload = PostTaskDecisionService(client=FakeClient()).decide(
            session=session,
            transcript="Scroll and open the first result",
            phase="completed",
            current_app_package="com.android.chrome",
            current_window_title="Search results",
        )

        self.assertEqual(payload["decision"], "continue_session")
        self.assertIn("Продължавам", payload["reply_message"])
        self.assertIn("Превърти", payload["next_instruction"])

    def test_service_falls_back_to_clarification_when_openai_fails(self):
        class FailingClient:
            def generate(self, **kwargs):
                raise LLMError("quota exceeded")

        session = SessionService.create(
            user_id="service-test",
            device_id="pixel-1",
            transcript="Open Viber",
            input_mode="text",
            reasoning_provider="openai",
            supported_packages=["com.viber.voip"],
        )
        session.store_intent_data(
            goal="Open Viber",
            target_app="com.viber.voip",
            entities={},
            risk_level="low",
        )

        payload = PostTaskDecisionService(client=FailingClient()).decide(
            session=session,
            transcript="Take me back now",
            phase="completed",
        )

        self.assertEqual(payload["decision"], "ask_for_clarification")
        self.assertTrue(payload["reply_message"])
