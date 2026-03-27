from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.agent_core.services.screen_formatter import (
    SENSITIVE_SENTINEL,
    format_screen_for_llm,
    summarize_step_history,
)
from apps.agent_core.services.step_reasoning import (
    ReasonedStep,
    StepReasoningService,
    _extract_refs,
    _normalize_reasoned_step,
    _validate_response,
)


def _node(
    ref: str,
    cls: str,
    *,
    text: str | None = None,
    cdesc: str | None = None,
    view_id: str | None = None,
    clickable: bool = False,
    editable: bool = False,
    focused: bool = False,
    bounds: dict | None = None,
    children: list[str] | None = None,
) -> dict:
    return {
        "ref": ref,
        "class_name": cls,
        "text": text,
        "content_desc": cdesc,
        "view_id": view_id,
        "clickable": clickable,
        "editable": editable,
        "focused": focused,
        "enabled": True,
        "bounds": bounds or {"left": 0, "top": 0, "right": 100, "bottom": 50},
        "children": children or [],
    }


def _screen(nodes: list[dict], *, focused: str | None = None, sensitive: bool = False) -> dict:
    return {
        "foreground_package": "com.android.chrome",
        "window_title": "Chrome",
        "screen_hash": "abc123",
        "focused_element_ref": focused,
        "is_sensitive": sensitive,
        "nodes": nodes,
    }


class ScreenFormatterTests(SimpleTestCase):
    def test_format_screen_basic(self):
        nodes = [
            _node("n0", "android.widget.FrameLayout", children=["n1", "n2"]),
            _node("n1", "android.widget.TextView", text="Headline", clickable=True),
            _node("n2", "android.widget.EditText", cdesc="Search", clickable=True, editable=True),
        ]
        for i in range(3, 10):
            nodes.append(_node(f"n{i}", "android.widget.TextView", text=f"Row {i}", clickable=True))

        text = format_screen_for_llm(_screen(nodes))
        self.assertIn("Foreground: com.android.chrome", text)
        self.assertIn("[n1]", text)
        self.assertIn("[n2]", text)

    def test_format_screen_pruning(self):
        nodes = [_node("root", "android.widget.FrameLayout", children=[f"n{i}" for i in range(1, 201)])]
        for i in range(1, 180):
            nodes.append(
                _node(
                    f"n{i}",
                    "android.view.View",
                    bounds={"left": 0, "top": 0, "right": 5, "bottom": 5},
                )
            )
        nodes.extend(
            [
                _node("n190", "android.widget.Button", text="Search", clickable=True, view_id="search_button"),
                _node("n191", "android.widget.EditText", cdesc="Search box", clickable=True, editable=True),
                _node("n192", "android.widget.TextView", text="Result", clickable=True),
            ]
        )

        text = format_screen_for_llm(_screen(nodes), token_budget=120)
        self.assertIn("[n190]", text)
        self.assertIn("[n191]", text)
        self.assertNotIn("[n5]", text)

    def test_format_screen_sensitive(self):
        text = format_screen_for_llm(_screen([_node("n1", "android.widget.TextView", text="Hidden")], sensitive=True))
        self.assertIn(SENSITIVE_SENTINEL, text)
        self.assertNotIn("[n1]", text)

    def test_summarize_history_recent(self):
        history = [
            {
                "step_index": i + 1,
                "action_type": "TAP_ELEMENT",
                "params": {"selector": {"element_ref": f"n{i}"}},
                "reasoning": f"Tap n{i}",
                "result_success": True,
                "result_code": "OK",
            }
            for i in range(5)
        ]
        text = summarize_step_history(history)
        self.assertEqual(text.count("Step "), 5)
        self.assertIn("Tap n4", text)

    def test_summarize_history_truncation(self):
        history = [
            {
                "step_index": i + 1,
                "action_type": "TAP_ELEMENT",
                "params": {"selector": {"element_ref": f"n{i}"}},
                "result_success": True,
                "result_code": "OK",
            }
            for i in range(20)
        ]
        text = summarize_step_history(history, max_steps=5)
        self.assertIn("Earlier steps:", text)
        self.assertLessEqual(text.count("Step "), 5)


class ValidationTests(SimpleTestCase):
    def test_validate_response_valid(self):
        screen_state = _screen([_node("n1", "android.widget.EditText", editable=True, focused=True)], focused="n1")
        raw = {
            "action_type": "TYPE_TEXT",
            "params": {"text": "query"},
            "reasoning": "n1 is focused.",
            "confidence": 0.8,
            "is_goal_complete": False,
            "requires_confirmation": False,
            "sensitivity": "medium",
        }
        self.assertIsNone(_validate_response(raw, _extract_refs(screen_state), screen_state))

    def test_validate_response_invalid_action(self):
        screen_state = _screen([_node("n1", "android.widget.Button", text="Go", clickable=True)])
        raw = {
            "action_type": "BLINK_ELEMENT",
            "params": {},
            "reasoning": "invalid",
            "confidence": 0.5,
            "is_goal_complete": False,
            "requires_confirmation": False,
            "sensitivity": "low",
        }
        error = _validate_response(raw, _extract_refs(screen_state), screen_state)
        self.assertIn("not valid", error or "")

    def test_validate_response_invalid_ref(self):
        screen_state = _screen([_node("n1", "android.widget.Button", text="Go", clickable=True)])
        raw = {
            "action_type": "TAP_ELEMENT",
            "params": {"selector": {"element_ref": "n404"}},
            "reasoning": "missing ref",
            "confidence": 0.5,
            "is_goal_complete": False,
            "requires_confirmation": False,
            "sensitivity": "low",
        }
        error = _validate_response(raw, _extract_refs(screen_state), screen_state)
        self.assertIn("element_ref", error or "")

    def test_validate_response_malformed_json(self):
        screen_state = _screen([_node("n1", "android.widget.Button", text="Go", clickable=True)])
        error = _validate_response("not-json", _extract_refs(screen_state), screen_state)
        self.assertIn("JSON object", error or "")


class StepReasoningServiceTests(SimpleTestCase):
    @patch("apps.agent_core.services.step_reasoning.LLMClient.from_settings")
    def test_reason_next_step_uses_mocked_llm(self, mock_from_settings):
        mock_client = MagicMock()
        mock_client.generate.return_value = {
            "action_type": "TYPE_TEXT",
            "params": {"text": "restaurants"},
            "reasoning": "The search field is focused.",
            "confidence": 0.91,
            "is_goal_complete": False,
            "requires_confirmation": False,
            "sensitivity": "medium",
        }
        mock_from_settings.return_value = mock_client

        service = StepReasoningService()
        result = service.reason_next_step(
            goal="Search for restaurants",
            target_app="com.android.chrome",
            entities={"query": "restaurants"},
            screen_state=_screen([_node("n1", "android.widget.EditText", editable=True, focused=True)], focused="n1"),
            step_history=[],
            constraints={"max_steps_remaining": 10},
        )

        self.assertEqual(result.action_type, "TYPE_TEXT")
        self.assertAlmostEqual(result.confidence, 0.91)

    def test_normalize_reasoned_step_prefers_focus_for_unfocused_editable(self):
        step = _normalize_reasoned_step(
            ReasonedStep(
                action_type="TAP_ELEMENT",
                params={"selector": {"element_ref": "n1"}},
                reasoning="The search bar is visible and clickable.",
                confidence=0.86,
                is_goal_complete=False,
                requires_confirmation=False,
                sensitivity="low",
            ),
            screen_state=_screen(
                [_node("n1", "android.widget.EditText", editable=True, clickable=True, focused=False)],
                focused=None,
            ),
        )
        self.assertEqual(step.action_type, "FOCUS_ELEMENT")
        self.assertIn("visible but not focused", step.reasoning)
