from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from apps.agent_core.services.screen_formatter import (
    SENSITIVE_SENTINEL,
    format_screen_for_llm,
    summarize_screen_for_history,
    summarize_step_history,
)
from apps.agent_core.services.step_reasoning import (
    ReasonedStep,
    StepReasoningService,
    _build_failure_context,
    _align_step_to_visible_text_target,
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
    long_clickable: bool = False,
    scrollable: bool = False,
    editable: bool = False,
    focused: bool = False,
    selected: bool = False,
    checkable: bool = False,
    checked: bool = False,
    parent_ref: str | None = None,
    index_in_parent: int = 0,
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
        "long_clickable": long_clickable,
        "scrollable": scrollable,
        "editable": editable,
        "focused": focused,
        "selected": selected,
        "checkable": checkable,
        "checked": checked,
        "enabled": True,
        "parent_ref": parent_ref,
        "index_in_parent": index_in_parent,
        "bounds": bounds or {"left": 0, "top": 0, "right": 100, "bottom": 50},
        "child_count": len(children or []),
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

    @override_settings(AGENT_UNSAFE_AUTOMATION_MODE=False)
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
                "screen_summary_after": f"Foreground=com.android.chrome | Window=Result {i}",
            }
            for i in range(5)
        ]
        text = summarize_step_history(history)
        self.assertEqual(text.count("Step "), 5)
        self.assertIn("Tap n4", text)
        self.assertIn("screen:", text)
        self.assertIn("Result 4", text)

    def test_summarize_history_keeps_full_action_trail_when_budget_allows(self):
        history = [
            {
                "step_index": i + 1,
                "action_type": "TAP_ELEMENT",
                "params": {"selector": {"element_ref": f"n{i}"}},
                "result_success": True,
                "result_code": "OK",
                "screen_summary_after": f"Foreground=com.android.chrome | Window=Result {i}",
            }
            for i in range(20)
        ]
        text = summarize_step_history(history, max_steps=5)
        self.assertIn("Step 1:", text)
        self.assertIn("Step 20:", text)

    def test_summarize_history_compacts_older_steps_under_small_budget(self):
        history = [
            {
                "step_index": i + 1,
                "action_type": "TAP_ELEMENT",
                "params": {"selector": {"element_ref": f"n{i}"}},
                "reasoning": f"Tap result {i}",
                "result_success": True,
                "result_code": "OK",
                "screen_summary_after": f"Foreground=com.android.chrome | Window=Result {i}",
            }
            for i in range(20)
        ]
        text = summarize_step_history(history, max_steps=5, token_budget=180)
        self.assertIn("Earlier steps:", text)
        self.assertIn("#1", text)
        self.assertIn("#20", text)

    def test_summarize_screen_for_history_redacts_editable_text_values(self):
        summary = summarize_screen_for_history(_screen([
            _node("n1", "android.widget.EditText", text="super secret message", editable=True, focused=True),
            _node("n2", "android.widget.ImageButton", cdesc="Send", clickable=True),
        ], focused="n1"))
        self.assertIn("EditText", summary)
        self.assertIn("Send", summary)
        self.assertNotIn("super secret message", summary)

    def test_format_screen_prefers_contact_name_over_section_header_label(self):
        text = format_screen_for_llm(_screen([
            _node("n13", "android.widget.RelativeLayout", clickable=True),
            _node("n14", "android.widget.TextView", text="CONTACTS"),
            _node("n15", "android.widget.TextView", text="Кичо"),
            _node("n16", "android.widget.ImageView", cdesc="Video call", clickable=True),
        ]))
        self.assertIn("label='Кичо'", text)

    def test_format_screen_surfaces_kind_actions_and_parent_metadata(self):
        text = format_screen_for_llm(_screen([
            _node(
                "n1",
                "android.widget.RelativeLayout",
                clickable=True,
                children=["n2"],
                bounds={"left": 0, "top": 200, "right": 300, "bottom": 320},
            ),
            _node(
                "n2",
                "android.widget.TextView",
                text="Alex",
                parent_ref="n1",
                index_in_parent=0,
                bounds={"left": 20, "top": 220, "right": 180, "bottom": 270},
            ),
            _node(
                "n3",
                "android.widget.EditText",
                cdesc="Message",
                clickable=True,
                editable=True,
                bounds={"left": 0, "top": 700, "right": 300, "bottom": 780},
            ),
        ]))
        self.assertIn("kind=row", text)
        self.assertIn("actions=tap", text)
        self.assertIn("kind=input", text)
        self.assertIn("actions=tap,focus,type", text)
        self.assertIn("parent=n1", text)
        self.assertIn("idx=0", text)

    def test_format_screen_keeps_view_id_as_metadata_not_title(self):
        text = format_screen_for_llm(_screen([
            _node(
                "n25",
                "android.widget.ImageView",
                view_id="com.nothing.camera:id/google_lens_btn",
                cdesc="Take Photo",
                clickable=True,
            ),
        ]))
        self.assertNotIn("title='", text)
        self.assertIn("id=com.nothing.camera:id/google_lens", text)
        self.assertIn("contentDesc='Take Photo'", text)


@override_settings(AGENT_UNSAFE_AUTOMATION_MODE=False)
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

    def test_validate_response_allows_get_screenshot(self):
        screen_state = _screen([_node("n1", "android.widget.TextView", text="Sparse UI")])
        raw = {
            "action_type": "GET_SCREENSHOT",
            "params": {"element_hint": "first search result row"},
            "reasoning": "No reliable target node is exposed after scanning the tree.",
            "confidence": 0.7,
            "is_goal_complete": False,
            "requires_confirmation": False,
            "sensitivity": "low",
        }
        self.assertIsNone(_validate_response(raw, _extract_refs(screen_state), screen_state))

    def test_validate_response_allows_request_user_input(self):
        screen_state = _screen([
            _node("n1", "android.widget.TextView", text="Alex Chen", clickable=True),
            _node("n2", "android.widget.TextView", text="Alex Johnson", clickable=True),
        ])
        raw = {
            "action_type": "REQUEST_USER_INPUT",
            "params": {
                "question": "Which Alex should I message? I see Alex Chen and Alex Johnson.",
                "required_fields": ["recipient"],
                "candidates": ["Alex Chen", "Alex Johnson"],
                "reason": "multiple_visible_matches",
                "max_attempts": 3,
            },
            "reasoning": "Two visible contacts match the requested recipient, so ask before continuing.",
            "confidence": 0.9,
            "is_goal_complete": False,
            "requires_confirmation": False,
            "sensitivity": "low",
        }
        self.assertIsNone(_validate_response(raw, _extract_refs(screen_state), screen_state))

    def test_validate_response_rejects_open_ended_request_user_input(self):
        screen_state = _screen([
            _node("n1", "android.widget.TextView", text="Alex Chen", clickable=True),
            _node("n2", "android.widget.TextView", text="Alex Johnson", clickable=True),
        ])
        raw = {
            "action_type": "REQUEST_USER_INPUT",
            "params": {
                "question": "What should I do next?",
                "required_fields": ["recipient"],
                "candidates": ["Alex Chen", "Alex Johnson"],
                "reason": "multiple_visible_matches",
                "max_attempts": 3,
            },
            "reasoning": "The next step is unclear.",
            "confidence": 0.8,
            "is_goal_complete": False,
            "requires_confirmation": False,
            "sensitivity": "low",
        }
        error = _validate_response(raw, _extract_refs(screen_state), screen_state)
        self.assertIn("too open-ended", error or "")

    def test_validate_response_rejects_confirmation_like_request_user_input(self):
        screen_state = _screen([
            _node("send_btn", "android.widget.ImageButton", cdesc="Send", clickable=True),
        ])
        raw = {
            "action_type": "REQUEST_USER_INPUT",
            "params": {
                "question": "Should I tap Send? Yes or no?",
                "required_fields": ["recipient"],
                "reason": "underdetermined_next_step",
                "max_attempts": 3,
            },
            "reasoning": "Ask for approval before sending.",
            "confidence": 0.8,
            "is_goal_complete": False,
            "requires_confirmation": False,
            "sensitivity": "low",
        }
        error = _validate_response(raw, _extract_refs(screen_state), screen_state)
        self.assertIn("approval or yes/no consent", error or "")

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

    def test_validate_response_rejects_horizontal_scroll_direction(self):
        screen_state = _screen([_node("n1", "androidx.recyclerview.widget.RecyclerView", scrollable=True)])
        raw = {
            "action_type": "SCROLL",
            "params": {"direction": "left"},
            "reasoning": "Move left.",
            "confidence": 0.7,
            "is_goal_complete": False,
            "requires_confirmation": False,
            "sensitivity": "low",
        }
        error = _validate_response(raw, _extract_refs(screen_state), screen_state)
        self.assertIn("params.direction", error or "")

    def test_validate_response_malformed_json(self):
        screen_state = _screen([_node("n1", "android.widget.Button", text="Go", clickable=True)])
        error = _validate_response("not-json", _extract_refs(screen_state), screen_state)
        self.assertIn("JSON object", error or "")

    def test_format_screen_includes_explicit_state_flags(self):
        text = format_screen_for_llm(_screen([
            _node(
                "n12",
                "androidx.recyclerview.widget.RecyclerView",
                view_id="com.viber.voip:id/recycler_view",
                clickable=False,
                editable=False,
                focused=False,
            )
        ]))
        self.assertIn("clickable=false", text)
        self.assertIn("editable=false", text)
        self.assertIn("focused=false", text)
        self.assertIn("enabled=true", text)

    def test_format_screen_surfaces_descendant_label_for_clickable_row(self):
        text = format_screen_for_llm(_screen([
            _node("n0", "android.widget.FrameLayout", children=["n1"]),
            _node("n1", "android.view.ViewGroup", clickable=True, children=["n2", "n3"]),
            _node("n2", "android.widget.TextView", text="Кичо"),
            _node("n3", "android.widget.ImageView", cdesc="Video call", clickable=True),
        ]))
        self.assertIn("[n1]", text)
        self.assertIn("label='Кичо'", text)

    def test_format_screen_surfaces_flat_sibling_label_for_clickable_row(self):
        text = format_screen_for_llm(_screen([
            _node("n8", "android.view.ViewGroup", clickable=True),
            _node("n9", "android.widget.TextView", text="Майката"),
            _node("n12", "android.view.ViewGroup", clickable=True),
            _node("n13", "android.widget.TextView", text="Кичо"),
        ]))
        self.assertIn("label='Майката'", text)
        self.assertIn("label='Кичо'", text)

    @override_settings(AGENT_UNSAFE_AUTOMATION_MODE=True)
    def test_validate_response_rejects_abort_in_unsafe_mode(self):
        screen_state = _screen([_node("n1", "android.widget.Button", text="Go", clickable=True)])
        raw = {
            "action_type": "ABORT",
            "params": {"reason": "sensitive_screen"},
            "reasoning": "Unsafe mode should not accept abort here.",
            "confidence": 0.5,
            "is_goal_complete": False,
            "requires_confirmation": False,
            "sensitivity": "high",
        }

        error = _validate_response(raw, _extract_refs(screen_state), screen_state)
        self.assertIn("Unsafe automation mode is enabled", error or "")

    def test_build_failure_context_surfaces_stale_screen_hint(self):
        context = _build_failure_context([
            {
                "action_type": "TAP_ELEMENT",
                "params": {"selector": {"element_ref": "n25"}},
                "result_success": False,
                "result_code": "NO_SCREEN_CHANGE",
            }
        ])
        self.assertIn("Screen did not change after this action", context)
        self.assertIn("n25", context)


@override_settings(AGENT_UNSAFE_AUTOMATION_MODE=False)
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

    @patch("apps.agent_core.services.step_reasoning.LLMClient.from_settings")
    def test_reason_next_step_emits_clarification_when_required_entity_is_missing(self, mock_from_settings):
        mock_client = MagicMock()
        mock_client.generate.return_value = {
            "action_type": "REQUEST_USER_INPUT",
            "params": {
                "question": "Where should I navigate?",
                "required_fields": ["destination"],
                "reason": "missing_required_data",
                "max_attempts": 3,
            },
            "reasoning": "A destination is required before the next safe step can be chosen.",
            "confidence": 0.93,
            "is_goal_complete": False,
            "requires_confirmation": False,
            "sensitivity": "low",
        }
        mock_from_settings.return_value = mock_client

        service = StepReasoningService()
        result = service.reason_next_step(
            goal="Start navigation",
            target_app="com.google.android.apps.maps",
            entities={},
            screen_state=_screen([
                _node("n1", "android.widget.ImageButton", cdesc="Search here", clickable=True),
                _node("n2", "android.widget.TextView", text="Home", clickable=True),
            ]),
            step_history=[],
            constraints={"max_steps_remaining": 8},
        )

        self.assertEqual(result.action_type, "REQUEST_USER_INPUT")
        self.assertEqual(result.params["required_fields"], ["destination"])
        self.assertEqual(result.params["reason"], "missing_required_data")

    @patch("apps.agent_core.services.step_reasoning.LLMClient.from_settings")
    def test_reason_next_step_emits_clarification_for_multiple_visible_candidates(self, mock_from_settings):
        mock_client = MagicMock()
        mock_client.generate.return_value = {
            "action_type": "REQUEST_USER_INPUT",
            "params": {
                "question": "Which Alex should I message? I see Alex Chen and Alex Johnson.",
                "required_fields": ["recipient"],
                "candidates": ["Alex Chen", "Alex Johnson"],
                "reason": "multiple_visible_matches",
                "max_attempts": 3,
            },
            "reasoning": "Two visible contacts plausibly match Alex, so clarification is safer than guessing.",
            "confidence": 0.95,
            "is_goal_complete": False,
            "requires_confirmation": False,
            "sensitivity": "low",
        }
        mock_from_settings.return_value = mock_client

        service = StepReasoningService()
        result = service.reason_next_step(
            goal="Message Alex on WhatsApp",
            target_app="com.whatsapp",
            entities={"recipient": "Alex"},
            screen_state=_screen([
                _node("n1", "android.widget.TextView", text="Alex Chen", clickable=True),
                _node("n2", "android.widget.TextView", text="Alex Johnson", clickable=True),
            ]),
            step_history=[],
            constraints={"max_steps_remaining": 8},
        )

        self.assertEqual(result.action_type, "REQUEST_USER_INPUT")
        self.assertEqual(result.params["candidates"], ["Alex Chen", "Alex Johnson"])
        self.assertEqual(result.params["reason"], "multiple_visible_matches")

    @patch("apps.agent_core.services.step_reasoning.LLMClient.from_settings")
    def test_reason_next_step_does_not_auto_complete_send_goal(self, mock_from_settings):
        mock_client = MagicMock()
        mock_client.generate.return_value = {
            "action_type": "TAP_ELEMENT",
            "params": {"selector": {"element_ref": "send_btn"}},
            "reasoning": "The draft is ready to send.",
            "confidence": 0.88,
            "is_goal_complete": False,
            "requires_confirmation": False,
            "sensitivity": "low",
        }
        mock_from_settings.return_value = mock_client

        screen_state = _screen(
            [
                _node("title", "android.widget.TextView", text="Alex"),
                _node("message_box", "android.widget.EditText", text="Running late", clickable=True, editable=True, focused=True),
                _node("send_btn", "android.widget.ImageButton", cdesc="Send", clickable=True),
            ],
            focused="message_box",
        )
        screen_state["foreground_package"] = "com.whatsapp"
        screen_state["window_title"] = "Alex"

        service = StepReasoningService()
        result = service.reason_next_step(
            goal="Send Alex a WhatsApp message",
            target_app="com.whatsapp",
            entities={"recipient": "Alex"},
            screen_state=screen_state,
            step_history=[{"action_type": "TYPE_TEXT", "result_success": True}],
            constraints={"max_steps_remaining": 3},
        )

        self.assertFalse(result.is_goal_complete)

    @patch("apps.agent_core.services.step_reasoning.LLMClient.from_settings")
    def test_reason_next_step_preserves_model_completion_decision(self, mock_from_settings):
        mock_client = MagicMock()
        mock_client.generate.return_value = {
            "action_type": "TAP_ELEMENT",
            "params": {"selector": {"element_ref": "send_btn"}},
            "reasoning": "The requested chat is already open.",
            "confidence": 0.93,
            "is_goal_complete": True,
            "requires_confirmation": False,
            "sensitivity": "low",
        }
        mock_from_settings.return_value = mock_client

        screen_state = _screen(
            [
                _node("title", "android.widget.TextView", text="Alex"),
                _node("message_box", "android.widget.EditText", cdesc="Message", clickable=True, editable=True),
                _node("send_btn", "android.widget.ImageButton", cdesc="Send", clickable=True),
            ]
        )
        screen_state["foreground_package"] = "com.viber.voip"
        screen_state["window_title"] = "Alex"

        service = StepReasoningService()
        result = service.reason_next_step(
            goal="Open the Alex chat in Viber",
            target_app="com.viber.voip",
            entities={"recipient": "Alex"},
            screen_state=screen_state,
            step_history=[{"action_type": "OPEN_APP", "result_success": True}],
            constraints={"max_steps_remaining": 6},
        )

        self.assertTrue(result.is_goal_complete)

    @patch("apps.agent_core.services.step_reasoning.LLMClient.from_settings")
    def test_reason_next_step_does_not_auto_complete_alarm_setup_goal(self, mock_from_settings):
        mock_client = MagicMock()
        mock_client.generate.return_value = {
            "action_type": "TAP_ELEMENT",
            "params": {"selector": {"element_ref": "n20"}},
            "reasoning": "Tap Add alarm to start creating the requested alarm.",
            "confidence": 0.95,
            "is_goal_complete": False,
            "requires_confirmation": False,
            "sensitivity": "low",
        }
        mock_from_settings.return_value = mock_client

        screen_state = {
            "foreground_package": "com.google.android.deskclock",
            "window_title": "Clock",
            "screen_hash": "clock_add",
            "focused_element_ref": None,
            "is_sensitive": False,
            "nodes": [
                _node("n20", "android.widget.Button", text="Add alarm", clickable=True),
                _node("n21", "android.widget.TextView", text="Alarm"),
            ],
        }

        service = StepReasoningService()
        result = service.reason_next_step(
            goal="Open Clock app and set alarm for 7:00 tomorrow",
            target_app="com.google.android.deskclock",
            entities={"time": "7:00", "day": "tomorrow"},
            screen_state=screen_state,
            step_history=[{"action_type": "OPEN_APP", "result_success": True}],
            constraints={"max_steps_remaining": 6},
        )

        self.assertFalse(result.is_goal_complete)
        self.assertEqual(result.action_type, "TAP_ELEMENT")
        self.assertEqual(result.params["selector"]["element_ref"], "n20")

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

    def test_align_step_to_visible_text_target_retargets_wrong_row(self):
        screen_state = _screen([
            _node("n8", "android.view.ViewGroup", clickable=True),
            _node("n9", "android.widget.TextView", text="Майката"),
            _node("n12", "android.view.ViewGroup", clickable=True),
            _node("n13", "android.widget.TextView", text="Кичо"),
        ])
        step = _align_step_to_visible_text_target(
            ReasonedStep(
                action_type="TAP_ELEMENT",
                params={"selector": {"element_ref": "n8"}},
                reasoning="Tap the first result row.",
                confidence=0.7,
                is_goal_complete=False,
                requires_confirmation=False,
                sensitivity="low",
            ),
            screen_state=screen_state,
            entities={"query": "Кичо"},
            goal="Search for Кичо in Viber",
        )
        self.assertEqual(step.params["selector"]["element_ref"], "n12")
        self.assertIn("matches the requested text", step.reasoning)

    def test_align_step_to_visible_text_target_retargets_grouped_contact_to_text_node(self):
        screen_state = _screen([
            _node("n13", "android.widget.RelativeLayout", clickable=True),
            _node("n14", "android.widget.TextView", text="CONTACTS"),
            _node("n15", "android.widget.TextView", text="Кичо"),
            _node("n16", "android.widget.ImageView", cdesc="Video call", clickable=True),
        ])
        step = _align_step_to_visible_text_target(
            ReasonedStep(
                action_type="TAP_ELEMENT",
                params={"selector": {"element_ref": "n13"}},
                reasoning="Tap the contacts row.",
                confidence=0.7,
                is_goal_complete=False,
                requires_confirmation=False,
                sensitivity="low",
            ),
            screen_state=screen_state,
            entities={"query": "Кичо"},
            goal="Search for Кичо in Viber",
        )
        self.assertEqual(step.params["selector"]["element_ref"], "n13")
        self.assertIn("matches the requested text", step.reasoning)

    def test_align_step_to_visible_text_target_keeps_exact_clickable_row(self):
        screen_state = _screen([
            _node("n36", "android.view.ViewGroup", clickable=True, children=["n37"]),
            _node(
                "n37",
                "android.widget.TextView",
                text="ÐšÐ¸Ñ‡Ð¾",
                parent_ref="n36",
                index_in_parent=0,
            ),
        ])
        step = _align_step_to_visible_text_target(
            ReasonedStep(
                action_type="TAP_ELEMENT",
                params={"selector": {"element_ref": "n36"}},
                reasoning="Tap the Kicho row.",
                confidence=0.7,
                is_goal_complete=False,
                requires_confirmation=False,
                sensitivity="low",
            ),
            screen_state=screen_state,
            entities={"query": "ÐšÐ¸Ñ‡Ð¾"},
            goal="Search for ÐšÐ¸Ñ‡Ð¾ in Viber",
        )
        self.assertEqual(step.params["selector"]["element_ref"], "n36")

    def test_align_step_to_visible_text_target_keeps_clickable_chrome_suggestion_row(self):
        screen_state = _screen([
            _node(
                "n6",
                "android.widget.EditText",
                text="Jeffrey Epstiena",
                clickable=True,
                editable=True,
                focused=True,
                bounds={"left": 0, "top": 40, "right": 300, "bottom": 120},
            ),
            _node(
                "n12",
                "android.view.ViewGroup",
                clickable=True,
                bounds={"left": 0, "top": 160, "right": 300, "bottom": 240},
            ),
            _node(
                "n13",
                "android.widget.TextView",
                text="jeffrey epstein",
                bounds={"left": 24, "top": 178, "right": 240, "bottom": 220},
            ),
        ], focused="n6")
        step = _align_step_to_visible_text_target(
            ReasonedStep(
                action_type="TAP_ELEMENT",
                params={"selector": {"element_ref": "n12"}},
                reasoning="Tap the matching suggestion row.",
                confidence=0.7,
                is_goal_complete=False,
                requires_confirmation=False,
                sensitivity="low",
            ),
            screen_state=screen_state,
            entities={"query": "jeffrey epstein"},
            goal="Search for jeffrey epstein in Chrome",
        )
        self.assertEqual(step.params["selector"]["element_ref"], "n12")

    def test_align_step_to_visible_text_target_does_not_retarget_to_chat_title(self):
        screen_state = _screen([
            _node(
                "n1",
                "android.view.ViewGroup",
                clickable=True,
                view_id="com.viber.voip:id/toolbar",
                bounds={"left": 0, "top": 0, "right": 300, "bottom": 120},
                children=["n2"],
            ),
            _node(
                "n2",
                "android.widget.TextView",
                text="Alex",
                view_id="com.viber.voip:id/title",
                parent_ref="n1",
                index_in_parent=0,
                bounds={"left": 90, "top": 30, "right": 220, "bottom": 80},
            ),
            _node(
                "n3",
                "android.widget.EditText",
                cdesc="Message…",
                clickable=True,
                editable=True,
                bounds={"left": 0, "top": 820, "right": 300, "bottom": 900},
            ),
        ])
        step = _align_step_to_visible_text_target(
            ReasonedStep(
                action_type="TAP_ELEMENT",
                params={"selector": {"element_ref": "n3"}},
                reasoning="Focus the message composer.",
                confidence=0.7,
                is_goal_complete=False,
                requires_confirmation=False,
                sensitivity="low",
            ),
            screen_state=screen_state,
            entities={"recipient": "Alex"},
            goal="Text Alex in Viber",
        )
        self.assertEqual(step.params["selector"]["element_ref"], "n3")
