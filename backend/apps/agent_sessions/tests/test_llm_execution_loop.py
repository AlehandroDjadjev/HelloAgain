from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.agent_core.llm_client import LLMError
from apps.agent_sessions.confirmation_service import ConfirmationService
from apps.agent_sessions.execution_service import CIRCUIT_BREAKER_THRESHOLD, ExecutionService
from apps.agent_sessions.models import ConfirmationRecord, SessionStatus
from apps.agent_sessions.services import SessionService
from apps.audit_log.models import AuditRecord


def _screen(
    package: str,
    title: str,
    screen_hash: str,
    nodes: list[dict],
    *,
    focused: str | None = None,
) -> dict:
    return {
        "foreground_package": package,
        "window_title": title,
        "screen_hash": screen_hash,
        "focused_element_ref": focused,
        "is_sensitive": False,
        "nodes": nodes,
    }


def _node(
    ref: str,
    cls: str,
    *,
    text: str | None = None,
    cdesc: str | None = None,
    clickable: bool = False,
    editable: bool = False,
    focused: bool = False,
) -> dict:
    return {
        "ref": ref,
        "class_name": cls,
        "text": text,
        "content_desc": cdesc,
        "clickable": clickable,
        "editable": editable,
        "focused": focused,
        "enabled": True,
        "bounds": {"left": 0, "top": 0, "right": 100, "bottom": 50},
        "children": [],
    }


class LLMExecutionLoopTests(TestCase):
    def _make_session(self, goal: str, target_app: str):
        session = SessionService.create(
            user_id="test_user",
            device_id="test_device",
            transcript=goal,
            input_mode="text",
            supported_packages=[target_app],
        )
        session.store_intent_data(goal=goal, target_app=target_app, entities={})
        SessionService.transition(session, SessionStatus.EXECUTING)
        return session

    @patch("apps.agent_core.services.step_reasoning.LLMClient.from_reasoning_provider")
    def test_chrome_search_e2e(self, mock_from_reasoning_provider):
        mock_client = MagicMock()
        mock_client.generate.side_effect = [
            {
                "action_type": "OPEN_APP",
                "params": {"package_name": "com.android.chrome"},
                "reasoning": "Chrome is not open yet.",
                "confidence": 0.95,
                "is_goal_complete": False,
                "requires_confirmation": False,
                "sensitivity": "low",
            },
            {
                "action_type": "TAP_ELEMENT",
                "params": {"selector": {"element_ref": "url_bar"}},
                "reasoning": "The URL bar is visible.",
                "confidence": 0.91,
                "is_goal_complete": False,
                "requires_confirmation": False,
                "sensitivity": "low",
            },
            {
                "action_type": "TYPE_TEXT",
                "params": {"text": "Jeffrey Epstein"},
                "reasoning": "The URL bar is focused.",
                "confidence": 0.89,
                "is_goal_complete": False,
                "requires_confirmation": False,
                "sensitivity": "low",
            },
            {
                "action_type": "TAP_ELEMENT",
                "params": {"selector": {"element_ref": "result_0"}},
                "reasoning": "Search results are visible, so the goal is complete.",
                "confidence": 0.93,
                "is_goal_complete": True,
                "requires_confirmation": False,
                "sensitivity": "low",
            },
        ]
        mock_from_reasoning_provider.return_value = mock_client

        session = self._make_session("Search Jeffrey Epstein on Chrome", "com.android.chrome")
        screens = [
            _screen("com.android.launcher", "Home", "home", [_node("chrome_icon", "android.widget.TextView", text="Chrome", clickable=True)]),
            _screen("com.android.chrome", "Chrome", "newtab", [_node("url_bar", "android.widget.EditText", cdesc="Search or type web address", clickable=True, editable=True)]),
            _screen("com.android.chrome", "Chrome", "focused", [_node("url_bar", "android.widget.EditText", cdesc="Search or type web address", editable=True, focused=True)], focused="url_bar"),
            _screen("com.android.chrome", "Search results", "results", [_node("result_0", "android.widget.TextView", text="Jeffrey Epstein - Wikipedia", clickable=True)]),
        ]

        first = ExecutionService.get_next_action(session, plan=None, screen_state=screens[0])
        self.assertEqual(first.status, "execute")
        self.assertEqual(first.next_action["type"], "OPEN_APP")
        ExecutionService.decide_after_result(
            session,
            plan=None,
            action_id=first.next_action["id"],
            result_success=True,
            result_code="OK",
            action_type=first.next_action["type"],
            params=first.next_action["params"],
            reasoning=first.reasoning,
            screen_hash_before="home",
            screen_hash_after="newtab",
        )

        second = ExecutionService.get_next_action(session, plan=None, screen_state=screens[1])
        self.assertEqual(second.status, "execute")
        self.assertEqual(second.next_action["type"], "FOCUS_ELEMENT")
        candidates = second.next_action["params"].get("selector_candidates", [])
        self.assertTrue(candidates)
        self.assertIn(
            {
                "class_name": "android.widget.EditText",
                "content_desc": "Search or type web address",
                "enabled": True,
            },
            candidates,
        )
        ExecutionService.decide_after_result(
            session,
            plan=None,
            action_id=second.next_action["id"],
            result_success=True,
            result_code="OK",
            action_type=second.next_action["type"],
            params=second.next_action["params"],
            reasoning=second.reasoning,
            screen_hash_before="newtab",
            screen_hash_after="focused",
        )

        third = ExecutionService.get_next_action(session, plan=None, screen_state=screens[2])
        self.assertEqual(third.status, "execute")
        self.assertEqual(third.next_action["type"], "TYPE_TEXT")
        ExecutionService.decide_after_result(
            session,
            plan=None,
            action_id=third.next_action["id"],
            result_success=True,
            result_code="OK",
            action_type=third.next_action["type"],
            params=third.next_action["params"],
            reasoning=third.reasoning,
            screen_hash_before="focused",
            screen_hash_after="results",
        )

        final = ExecutionService.get_next_action(session, plan=None, screen_state=screens[3])
        self.assertEqual(final.status, "complete")
        session.refresh_from_db()
        self.assertEqual(len(session.step_history), 3)
        self.assertGreater(AuditRecord.objects.filter(session=session).count(), 0)

    @patch("apps.agent_core.services.step_reasoning.LLMClient.from_reasoning_provider")
    def test_whatsapp_send_with_confirmation(self, mock_from_reasoning_provider):
        mock_client = MagicMock()
        mock_client.generate.side_effect = [
            {
                "action_type": "REQUEST_CONFIRMATION",
                "params": {
                    "prompt": "Send 'Running late' to Alex?",
                    "action_summary": "Tap Send in WhatsApp",
                },
                "reasoning": "The draft is ready and needs approval before sending.",
                "confidence": 0.98,
                "is_goal_complete": False,
                "requires_confirmation": True,
                "sensitivity": "high",
            },
            {
                "action_type": "TAP_ELEMENT",
                "params": {"selector": {"element_ref": "sent_indicator"}},
                "reasoning": "The sent indicator is visible, so the goal is complete.",
                "confidence": 0.9,
                "is_goal_complete": True,
                "requires_confirmation": False,
                "sensitivity": "low",
            },
        ]
        mock_from_reasoning_provider.return_value = mock_client

        session = self._make_session("Send Alex a WhatsApp message", "com.whatsapp")
        compose_screen = _screen(
            "com.whatsapp",
            "Alex",
            "compose",
            [
                _node("message_box", "android.widget.EditText", text="Running late", editable=True, focused=True),
                _node("send_btn", "android.widget.ImageButton", cdesc="Send", clickable=True),
            ],
            focused="message_box",
        )
        sent_screen = _screen(
            "com.whatsapp",
            "Alex",
            "sent",
            [_node("sent_indicator", "android.widget.TextView", text="Sent", clickable=False)],
        )

        response = ExecutionService.get_next_action(session, plan=None, screen_state=compose_screen)
        self.assertEqual(response.status, "confirm")
        pending = ConfirmationRecord.objects.get(session=session, status=ConfirmationRecord.Status.PENDING)
        self.assertIn("Tap Send", pending.action_summary)

        ConfirmationService.resolve(pending.id, approved=True, session=session)
        session.refresh_from_db()
        self.assertEqual(session.status, SessionStatus.EXECUTING)

        resumed = ExecutionService.get_next_action(session, plan=None, screen_state=sent_screen)
        self.assertEqual(resumed.status, "complete")

    @patch("apps.agent_core.services.step_reasoning.LLMClient.from_reasoning_provider")
    def test_circuit_breaker_trips(self, mock_from_reasoning_provider):
        mock_client = MagicMock()
        mock_client.generate.return_value = {
            "action_type": "TAP_ELEMENT",
            "params": {"selector": {"element_ref": "retry_btn"}},
            "reasoning": "Try the visible button.",
            "confidence": 0.7,
            "is_goal_complete": False,
            "requires_confirmation": False,
            "sensitivity": "low",
        }
        mock_from_reasoning_provider.return_value = mock_client

        session = self._make_session("Retry until failure", "com.android.chrome")
        screen = _screen(
            "com.android.chrome",
            "Chrome",
            "retry",
            [_node("retry_btn", "android.widget.Button", text="Retry", clickable=True)],
        )

        for _ in range(CIRCUIT_BREAKER_THRESHOLD):
            response = ExecutionService.get_next_action(session, plan=None, screen_state=screen)
            self.assertEqual(response.status, "execute")
            ExecutionService.decide_after_result(
                session,
                plan=None,
                action_id=response.next_action["id"],
                result_success=False,
                result_code="ELEMENT_NOT_FOUND",
                action_type=response.next_action["type"],
                params=response.next_action["params"],
                reasoning=response.reasoning,
                screen_hash_before="retry",
                screen_hash_after="retry",
            )

        final = ExecutionService.get_next_action(session, plan=None, screen_state=screen)
        self.assertEqual(final.status, "manual_takeover")

    @patch("apps.agent_core.services.step_reasoning.LLMClient.from_reasoning_provider")
    def test_llm_failure_fallback(self, mock_from_reasoning_provider):
        mock_client = MagicMock()
        mock_client.generate.side_effect = LLMError("timeout")
        mock_from_reasoning_provider.return_value = mock_client

        session = self._make_session("Search on Chrome", "com.android.chrome")
        screen = _screen("com.android.launcher", "Home", "home", [_node("chrome_icon", "android.widget.TextView", text="Chrome", clickable=True)])

        response = ExecutionService.get_next_action(session, plan=None, screen_state=screen)
        self.assertEqual(response.status, "execute")
        self.assertEqual(response.next_action["type"], "OPEN_APP")
        self.assertIn("LLM unavailable", response.reasoning)

    def test_failed_action_decision_includes_reason(self):
        session = self._make_session("Search on Chrome", "com.android.chrome")

        decision = ExecutionService.decide_after_result(
            session,
            plan=None,
            action_id="llm_test_fail",
            result_success=False,
            result_code="ELEMENT_NOT_CLICKABLE",
            result_message="Search bar exists but is not clickable",
            action_type="TAP_ELEMENT",
            params={"selector": {"element_ref": "url_bar"}},
            reasoning="Tap the search bar.",
            screen_hash_before="newtab",
            screen_hash_after="newtab",
        )

        self.assertEqual(decision.status, "continue")
        self.assertEqual(
            decision.reason,
            "ELEMENT_NOT_CLICKABLE: Search bar exists but is not clickable",
        )
