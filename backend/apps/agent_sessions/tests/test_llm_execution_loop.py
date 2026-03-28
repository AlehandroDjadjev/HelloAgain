from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.agent_core.llm_client import LLMError
from apps.agent_core.services.step_reasoning import ReasonedStep
from apps.agent_core.services.vision_reasoning import VisionTapTarget
from apps.agent_sessions.confirmation_service import ConfirmationService
from apps.agent_sessions.execution_service import (
    CIRCUIT_BREAKER_THRESHOLD,
    ExecutionService,
    _build_action_from_reasoned,
    _augment_selector_params,
    _node_selector_candidates,
)
from apps.agent_sessions.models import ConfirmationRecord, SessionStatus
from apps.agent_sessions.services import SessionService
from apps.audit_log.models import AuditRecord
from apps.device_bridge.services import store_screenshot


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


@override_settings(AGENT_UNSAFE_AUTOMATION_MODE=False)
class LLMExecutionLoopTests(TestCase):
    def test_coordinate_taps_are_transported_as_swipe_for_client_compat(self):
        reasoned = ReasonedStep(
            action_type="TAP_COORDINATES",
            params={"x": 540, "y": 600},
            reasoning="Tap the visible target by coordinates.",
            confidence=0.6,
            is_goal_complete=False,
            requires_confirmation=False,
            sensitivity="low",
        )

        action = _build_action_from_reasoned(reasoned)

        self.assertEqual(action["type"], "SWIPE")
        self.assertEqual(
            action["params"],
            {
                "start_x": 540,
                "start_y": 600,
                "end_x": 540,
                "end_y": 600,
                "duration_ms": 50,
            },
        )

    def test_node_selector_candidates_include_container_fallbacks(self):
        container_node = {
            "ref": "n12",
            "class_name": "androidx.recyclerview.widget.RecyclerView",
            "view_id": "com.viber.voip:id/recycler_view",
            "text": "",
            "content_desc": "",
            "clickable": False,
            "editable": False,
            "enabled": True,
        }

        self.assertIn(
            {"view_id": "com.viber.voip:id/recycler_view", "enabled": True},
            _node_selector_candidates(container_node),
        )

    def test_node_selector_candidates_prioritize_exact_text_identity(self):
        label_node = {
            "ref": "n37",
            "class_name": "android.widget.TextView",
            "view_id": "com.viber.voip:id/from",
            "text": "Кичо",
            "content_desc": "",
            "clickable": False,
            "editable": False,
            "enabled": True,
        }

        candidates = _node_selector_candidates(label_node)
        self.assertEqual(
            candidates[0],
            {
                "class_name": "android.widget.TextView",
                "view_id": "com.viber.voip:id/from",
                "text": "Кичо",
                "enabled": True,
            },
        )

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

    def test_get_next_action_returns_complete_for_completed_session(self):
        session = self._make_session("Open Chrome", "com.android.chrome")
        SessionService.transition(session, SessionStatus.COMPLETED)

        response = ExecutionService.get_next_action(
            session,
            plan=None,
            screen_state=_screen("com.android.chrome", "Chrome", "done", []),
        )

        self.assertEqual(response.status, "complete")
        self.assertEqual(response.reason, "Session is already complete.")

    def test_decide_after_result_returns_complete_for_completed_session(self):
        session = self._make_session("Open Chrome", "com.android.chrome")
        SessionService.transition(session, SessionStatus.COMPLETED)

        decision = ExecutionService.decide_after_result(
            session,
            plan=None,
            action_id="final_step",
            result_success=True,
            result_code="OK",
            action_type="OPEN_APP",
            params={"package_name": "com.android.chrome"},
            reasoning="The goal is already complete.",
            screen_hash_before="before",
            screen_hash_after="after",
        )

        self.assertEqual(decision.status, "complete")
        self.assertEqual(decision.reason, "Session is already complete.")

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

    def _non_clickable_text_target_prefers_generated_selector_candidates_first_legacy(self):
        params = {"selector": {"element_ref": "n37"}}
        screen_state = {
            "nodes": [
                {
                    "ref": "n37",
                    "class_name": "android.widget.TextView",
                    "view_id": "com.viber.voip:id/from",
                    "text": "Кичо",
                    "content_desc": "",
                    "clickable": False,
                    "editable": False,
                    "enabled": True,
                }
            ]
        }

        augmented = _augment_selector_params(
            "TAP_ELEMENT",
            params,
            screen_state=screen_state,
            target_app="com.viber.voip",
        )
        candidates = augmented.get("selector_candidates", [])
        self.assertTrue(candidates)
        self.assertEqual(
            candidates[0],
            {
                "class_name": "android.widget.TextView",
                "view_id": "com.viber.voip:id/from",
                "text": "ÐšÐ¸Ñ‡Ð¾",
                "enabled": True,
            },
        )
        self.assertEqual(candidates[-1], {"element_ref": "n37"})
        return

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

    def test_non_clickable_text_target_prefers_generated_selector_candidates_first(self):
        params = {"selector": {"element_ref": "n37"}}
        screen_state = {
            "nodes": [
                {
                    "ref": "n37",
                    "class_name": "android.widget.TextView",
                    "view_id": "com.viber.voip:id/from",
                    "text": "Kicho",
                    "content_desc": "",
                    "clickable": False,
                    "editable": False,
                    "enabled": True,
                }
            ]
        }

        augmented = _augment_selector_params(
            "TAP_ELEMENT",
            params,
            screen_state=screen_state,
            target_app="com.viber.voip",
        )
        candidates = augmented.get("selector_candidates", [])
        self.assertTrue(candidates)
        self.assertEqual(
            candidates[0],
            {
                "class_name": "android.widget.TextView",
                "view_id": "com.viber.voip:id/from",
                "text": "Kicho",
                "enabled": True,
            },
        )
        self.assertEqual(candidates[-1], {"element_ref": "n37"})

    @patch("apps.agent_core.services.step_reasoning.LLMClient.from_reasoning_provider")
    def test_open_app_waits_for_target_foreground_before_reasking_llm(self, mock_from_reasoning_provider):
        mock_client = MagicMock()
        mock_client.generate.side_effect = [
            {
                "action_type": "OPEN_APP",
                "params": {"package_name": "com.nothing.camera"},
                "reasoning": "Camera is not open yet.",
                "confidence": 0.95,
                "is_goal_complete": False,
                "requires_confirmation": False,
                "sensitivity": "low",
            },
            {
                "action_type": "TAP_ELEMENT",
                "params": {"selector": {"element_ref": "shutter"}},
                "reasoning": "The camera is in the foreground now, so tap the shutter.",
                "confidence": 0.92,
                "is_goal_complete": False,
                "requires_confirmation": False,
                "sensitivity": "low",
            },
        ]
        mock_from_reasoning_provider.return_value = mock_client

        session = self._make_session("Open Camera and take a photo", "com.nothing.camera")

        launcher_screen = _screen(
            "com.android.launcher",
            "Home",
            "home",
            [_node("camera_icon", "android.widget.TextView", text="Camera", clickable=True)],
        )
        stale_frontend_screen = _screen(
            "com.example.frontend",
            "frontend",
            "stale",
            [_node("status", "android.widget.TextView", text="Executing...")],
        )
        camera_screen = _screen(
            "com.nothing.camera",
            "Camera",
            "camera",
            [_node("shutter", "android.widget.ImageView", cdesc="Take Photo", clickable=True)],
        )

        first = ExecutionService.get_next_action(session, plan=None, screen_state=launcher_screen)
        self.assertEqual(first.status, "execute")
        self.assertEqual(first.next_action["type"], "OPEN_APP")

        decision = ExecutionService.decide_after_result(
            session,
            plan=None,
            action_id=first.next_action["id"],
            result_success=True,
            result_code="OK",
            action_type=first.next_action["type"],
            params=first.next_action["params"],
            reasoning=first.reasoning,
            screen_hash_before="home",
            screen_hash_after="home",
        )
        self.assertEqual(decision.status, "retry")

        waiting = ExecutionService.get_next_action(session, plan=None, screen_state=stale_frontend_screen)
        self.assertEqual(waiting.status, "retry")
        self.assertEqual(mock_client.generate.call_count, 1)

        resumed = ExecutionService.get_next_action(session, plan=None, screen_state=camera_screen)
        self.assertEqual(resumed.status, "execute")
        self.assertEqual(resumed.next_action["type"], "TAP_ELEMENT")
        self.assertEqual(mock_client.generate.call_count, 2)

    @patch("apps.agent_sessions.execution_service._get_vision_service")
    @patch("apps.agent_core.services.step_reasoning.LLMClient.from_reasoning_provider")
    def test_successful_llm_requested_screenshot_triggers_vision_followup(
        self,
        mock_from_reasoning_provider,
        mock_get_vision_service,
    ):
        mock_client = MagicMock()
        mock_client.generate.return_value = {
            "action_type": "GET_SCREENSHOT",
            "params": {"element_hint": "first visible search result row for Венци J"},
            "reasoning": "The contact row is not reliably exposed in the accessibility tree.",
            "confidence": 0.73,
            "is_goal_complete": False,
            "requires_confirmation": False,
            "sensitivity": "low",
        }
        mock_from_reasoning_provider.return_value = mock_client

        mock_vision = MagicMock()
        mock_vision.find_tap_target.return_value = VisionTapTarget(
            x=540,
            y=600,
            description="First search result row",
            confidence=0.8,
            reasoning="The first rendered result row matches the contact query.",
        )
        mock_get_vision_service.return_value = mock_vision

        session = self._make_session("Open the Венци J chat in Viber", "com.viber.voip")
        sparse_search_screen = _screen(
            "com.viber.voip",
            "Viber",
            "search",
            [
                _node("n1", "android.widget.ImageButton", cdesc="Collapse", clickable=True),
                _node("n3", "android.widget.AutoCompleteTextView", text="Венци J", focused=True),
                _node("n4", "android.widget.ImageView", cdesc="Clear query", clickable=True),
                _node("n6", "android.widget.LinearLayout", cdesc="Chats"),
                _node("n7", "android.widget.TextView", text="CHATS"),
            ],
            focused="n3",
        )

        first = ExecutionService.get_next_action(session, plan=None, screen_state=sparse_search_screen)
        self.assertEqual(first.status, "execute")
        self.assertEqual(first.next_action["type"], "GET_SCREENSHOT")

        store_screenshot(str(session.id), "fake-screenshot-b64")
        ExecutionService.decide_after_result(
            session,
            plan=None,
            action_id=first.next_action["id"],
            result_success=True,
            result_code="OK",
            action_type=first.next_action["type"],
            params=first.next_action["params"],
            reasoning=first.reasoning,
            screen_hash_before="search",
            screen_hash_after="search",
        )

        followup = ExecutionService.get_next_action(session, plan=None, screen_state=sparse_search_screen)
        self.assertEqual(followup.status, "execute")
        self.assertEqual(followup.next_action["type"], "SWIPE")
        self.assertEqual(followup.next_action["params"]["start_x"], 540)
        self.assertEqual(followup.next_action["params"]["start_y"], 600)
        mock_vision.find_tap_target.assert_called_once()

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

    def test_stale_tap_is_recorded_as_soft_failure(self):
        session = self._make_session("Tap a button on Chrome", "com.android.chrome")

        decision = ExecutionService.decide_after_result(
            session,
            plan=None,
            action_id="llm_stale_tap",
            result_success=True,
            result_code="OK",
            result_message="TAP",
            action_type="TAP_ELEMENT",
            params={"selector": {"element_ref": "retry_btn"}},
            reasoning="Tap the visible button.",
            screen_hash_before="same-screen",
            screen_hash_after="same-screen",
        )

        self.assertEqual(decision.status, "continue")
        self.assertIn("NO_SCREEN_CHANGE", decision.reason)
        self.assertIn("Screen did not change after this action", decision.reason)

        session.refresh_from_db()
        self.assertEqual(session.current_step_index, 0)
        self.assertFalse(session.step_history[-1]["result_success"])
        self.assertEqual(session.step_history[-1]["result_code"], "NO_SCREEN_CHANGE")
        self.assertIn(
            "Screen did not change after this action",
            session.step_history[-1]["reasoning"],
        )
