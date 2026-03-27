from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from apps.agent_core.services.screen_formatter import (
    SENSITIVE_SENTINEL,
    format_screen_for_llm,
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
        self.assertEqual(step.params["selector"]["element_ref"], "n13")
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
        self.assertEqual(step.params["selector"]["element_ref"], "n15")
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
