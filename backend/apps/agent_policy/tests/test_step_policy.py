from __future__ import annotations

from django.test import TestCase

from apps.agent_core.services.step_reasoning import ReasonedStep
from apps.agent_policy.models import UserAutomationPolicy
from apps.agent_policy.services import PolicyEnforcer


def _screen() -> dict:
    return {
        "foreground_package": "com.whatsapp",
        "window_title": "WhatsApp",
        "screen_hash": "hash123",
        "focused_element_ref": None,
        "is_sensitive": False,
        "nodes": [
            {
                "ref": "normal_btn",
                "class_name": "android.widget.Button",
                "text": "Open",
                "content_desc": "",
                "clickable": True,
                "enabled": True,
                "bounds": {"left": 0, "top": 0, "right": 100, "bottom": 50},
                "children": [],
            },
            {
                "ref": "send_btn",
                "class_name": "android.widget.Button",
                "text": "Send",
                "content_desc": "",
                "clickable": True,
                "enabled": True,
                "bounds": {"left": 0, "top": 60, "right": 100, "bottom": 110},
                "children": [],
            },
        ],
    }


def _step(action_type: str, params: dict, *, sensitivity: str = "low") -> ReasonedStep:
    return ReasonedStep(
        action_type=action_type,
        params=params,
        reasoning="test",
        confidence=0.8,
        is_goal_complete=False,
        requires_confirmation=False,
        sensitivity=sensitivity,
    )


class StepPolicyTests(TestCase):
    def test_allowed_action(self):
        result = PolicyEnforcer.check_step(
            step=_step("TAP_ELEMENT", {"selector": {"element_ref": "normal_btn"}}),
            session_goal="open the chat",
            target_package="com.whatsapp",
            user_policy=None,
            step_count=1,
            screen_state=_screen(),
        )
        self.assertTrue(result.allowed)
        self.assertFalse(result.requires_confirmation)

    def test_blocked_keyword_in_text(self):
        result = PolicyEnforcer.check_step(
            step=_step("TYPE_TEXT", {"text": "bank password"}, sensitivity="medium"),
            session_goal="fill a form",
            target_package="com.android.chrome",
            user_policy=None,
            step_count=1,
            screen_state=_screen(),
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.blocked_reason, "sensitive_content_detected")

    def test_blocked_package(self):
        result = PolicyEnforcer.check_step(
            step=_step("OPEN_APP", {"package_name": "com.unknown.app"}),
            session_goal="open unknown app",
            target_package="com.unknown.app",
            user_policy=None,
            step_count=1,
            screen_state=_screen(),
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.blocked_reason, "package_not_allowed")

    def test_confirmation_required_send(self):
        result = PolicyEnforcer.check_step(
            step=_step("TAP_ELEMENT", {"selector": {"element_ref": "send_btn"}}),
            session_goal="send a message",
            target_package="com.whatsapp",
            user_policy=None,
            step_count=1,
            screen_state=_screen(),
        )
        self.assertTrue(result.allowed)
        self.assertTrue(result.requires_confirmation)

    def test_user_policy_no_text(self):
        result = PolicyEnforcer.check_step(
            step=_step("TYPE_TEXT", {"text": "hello"}),
            session_goal="type hello",
            target_package="com.whatsapp",
            user_policy=UserAutomationPolicy(user_id="u1", allow_text_entry=False),
            step_count=1,
            screen_state=_screen(),
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.blocked_reason, "text_entry_not_allowed")

    def test_user_policy_narrows_packages(self):
        result = PolicyEnforcer.check_step(
            step=_step("TAP_ELEMENT", {"selector": {"element_ref": "normal_btn"}}),
            session_goal="browse chrome",
            target_package="com.android.chrome",
            user_policy=UserAutomationPolicy(user_id="u2", allowed_packages=["com.whatsapp"]),
            step_count=1,
            screen_state=_screen(),
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.blocked_reason, "package_not_allowlisted")

    def test_sensitive_escalation(self):
        policy = UserAutomationPolicy(
            user_id="u3",
            always_confirm_action_types=["TAP_ELEMENT"],
        )
        result = PolicyEnforcer.check_step(
            step=_step("TAP_ELEMENT", {"selector": {"element_ref": "normal_btn"}}, sensitivity="low"),
            session_goal="open the next screen",
            target_package="com.whatsapp",
            user_policy=policy,
            step_count=1,
            screen_state=_screen(),
        )
        self.assertTrue(result.allowed)
        self.assertTrue(result.requires_confirmation)
        self.assertEqual(result.modified_sensitivity, "medium")

    def test_unknown_action_blocked(self):
        result = PolicyEnforcer.check_step(
            step=_step("MAKE_MAGIC", {}),
            session_goal="do something impossible",
            target_package="com.whatsapp",
            user_policy=None,
            step_count=1,
            screen_state=_screen(),
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.blocked_reason, "invalid_action_type")
