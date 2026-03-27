"""
Smoke test for step reasoning, prompt construction, and per-step policy checks.

Run from backend/:
    python smoke_step_reasoning.py
"""
import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
sys.path.insert(0, os.path.dirname(__file__))

import django

django.setup()

from apps.agent_core.prompts.step_reasoning import build_step_reasoning_user_prompt
from apps.agent_core.services.screen_formatter import (
    SENSITIVE_SENTINEL,
    format_screen_for_llm,
    summarize_step_history,
)
from apps.agent_core.services.step_reasoning import (
    ReasonedStep,
    StepReasoningService,
    _extract_refs,
    _validate_response,
)
from apps.agent_core.llm_client import LLMError
from apps.agent_policy.models import UserAutomationPolicy
from apps.agent_policy.services import PolicyEnforcer

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append(cond)
    tag = PASS if cond else FAIL
    print(f"  [{tag}] {name}" + (f" -- {detail}" if detail and not cond else ""))


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


def _make_screen(
    nodes: list[dict] | None = None,
    *,
    is_sensitive: bool = False,
    focused: str | None = None,
    pkg: str = "com.android.chrome",
    title: str = "Chrome",
) -> dict:
    return {
        "foreground_package": pkg,
        "window_title": title,
        "screen_hash": "abc123",
        "focused_element_ref": focused,
        "is_sensitive": is_sensitive,
        "nodes": nodes or [],
    }


print("\n-- screen formatter --")

basic_nodes = [
    _node("n0", "android.widget.FrameLayout", children=["n1", "n2"]),
    _node("n1", "android.widget.TextView", text="Hello", clickable=True),
    _node("n2", "android.widget.EditText", cdesc="Search", clickable=True, editable=True, focused=True),
]
formatted = format_screen_for_llm(_make_screen(basic_nodes, focused="n2"))
check("1: formatter includes visible refs", "[n1]" in formatted and "[n2]" in formatted)
check("2: formatter includes compact header", formatted.startswith("Foreground: com.android.chrome"))

sensitive = format_screen_for_llm(_make_screen(basic_nodes, is_sensitive=True))
check("3: sensitive sentinel returned", SENSITIVE_SENTINEL in sensitive)
check("4: sensitive screen hides node refs", "[n1]" not in sensitive and "[n2]" not in sensitive)

layout_only = [
    _node("root", "android.widget.FrameLayout", children=["n1"]),
    _node("n1", "android.widget.TextView", text="Visible", clickable=True),
]
layout_text = format_screen_for_llm(_make_screen(layout_only))
check("5: empty layout node pruned", "FrameLayout" not in layout_text)
check("6: child survives hoisting", "[n1]" in layout_text)

many = [_node("list", "androidx.recyclerview.widget.RecyclerView", view_id="list", children=[f"n{i}" for i in range(10)])]
many.extend(_node(f"n{i}", "android.widget.TextView", text=f"Item {i}", clickable=True) for i in range(10))
many_text = format_screen_for_llm(_make_screen(many), max_nodes=50)
check("7: sibling list collapsed", "more TextView items" in many_text)

budget_nodes = [
    _node("n1", "android.widget.Button", text="Important", clickable=True, view_id="important"),
    _node("n2", "android.view.View", bounds={"left": 0, "top": 0, "right": 5, "bottom": 5}),
    _node("n3", "android.view.View", bounds={"left": 0, "top": 0, "right": 6, "bottom": 6}),
]
budget_text = format_screen_for_llm(_make_screen(budget_nodes), token_budget=30)
check("8: high relevance node kept under small budget", "[n1]" in budget_text)
check("9: tiny low relevance nodes dropped under budget", "[n2]" not in budget_text and "[n3]" not in budget_text)


print("\n-- history summary --")

history = [
    {"step_index": 1, "action_type": "OPEN_APP", "params": {"package_name": "com.android.chrome"}, "result_success": True, "result_code": "OK"},
    {"step_index": 2, "action_type": "TAP_ELEMENT", "params": {"selector": {"element_ref": "n5"}}, "result_success": True, "result_code": "OK"},
    {"step_index": 3, "action_type": "TYPE_TEXT", "params": {"text": "[5 chars]"}, "reasoning": "Typing query", "result_success": False, "result_code": "NO_FOCUS"},
]
summary = summarize_step_history(history)
check("10: empty history placeholder", summarize_step_history([]) == "(no steps yet)")
check("11: recent failure called out", "FAILED (NO_FOCUS)" in summary)
check("12: recent reasoning preserved", "Typing query" in summary)

long_history = [
    {"step_index": i + 1, "action_type": "TAP_ELEMENT", "params": {"selector": {"element_ref": f"n{i}"}}, "result_success": True, "result_code": "OK"}
    for i in range(9)
]
summary_long = summarize_step_history(long_history, max_steps=5)
check("13: older steps collapsed", "Earlier steps:" in summary_long)
check("14: only recent step lines retained", summary_long.count("Step ") <= 5)


print("\n-- prompt builder --")

prompt = build_step_reasoning_user_prompt(
    goal="Search for restaurants",
    target_app="com.android.chrome",
    entities={"query": "restaurants"},
    step_history_text=summary,
    constraints={"max_steps_remaining": 8, "policy_notes": "risk_level=low"},
    screen_header="Foreground: com.android.chrome | Window: Chrome | Focused: none | Visible nodes: 2",
    screen_tree="[n5] EditText contentDesc='Search or type web address' clickable editable",
    failure_context="TAP_ELEMENT on n5 returned NOT_FOUND. The element may have moved.",
    goal_progress="The target app is open and the task is in the first interaction phase.",
    app_context="ChromeExecutor classifies this screen as 'browser_open'. Common elements: address bar and page content.",
)
check("15: prompt contains progress section", "GOAL PROGRESS ESTIMATE:" in prompt)
check("16: prompt contains failure section", "LAST ACTION FAILED:" in prompt)
check("17: prompt contains app context", "APP CONTEXT:" in prompt)

retry_prompt = build_step_reasoning_user_prompt(
    goal="Test",
    target_app="com.android.chrome",
    entities={},
    step_history_text="(no steps yet)",
    constraints={"max_steps_remaining": 5},
    screen_header="Foreground: com.android.chrome",
    screen_tree="[n1] TextView 'x'",
    validation_error="Missing action_type field.",
)
check("18: prompt contains correction block", "CORRECTION REQUIRED:" in retry_prompt)


print("\n-- response validation --")

screen = _make_screen(
    [
        _node("n5", "android.widget.EditText", cdesc="Search or type web address", clickable=True, editable=True, focused=True),
    ],
    focused="n5",
)
valid_raw = {
    "action_type": "TYPE_TEXT",
    "params": {"text": "restaurants near me"},
    "reasoning": "The field is focused.",
    "confidence": 0.9,
    "is_goal_complete": False,
    "requires_confirmation": False,
    "sensitivity": "medium",
}
check("19: valid response passes validation", _validate_response(valid_raw, _extract_refs(screen), screen) is None)

bad_ref = {
    "action_type": "TAP_ELEMENT",
    "params": {"selector": {"element_ref": "n99"}},
    "reasoning": "Tap missing node",
    "confidence": 0.8,
    "is_goal_complete": False,
    "requires_confirmation": False,
    "sensitivity": "low",
}
check("20: bad ref rejected", "element_ref" in (_validate_response(bad_ref, _extract_refs(screen), screen) or ""))


print("\n-- step reasoning service --")

svc = StepReasoningService()
svc._llm = MagicMock()
svc._llm.generate.return_value = valid_raw
result = svc.reason_next_step(
    goal="Search for restaurants",
    target_app="com.android.chrome",
    entities={"query": "restaurants"},
    screen_state=screen,
    step_history=[],
    constraints={"max_steps_remaining": 10},
)
check("21: valid llm response becomes reasoned step", result.action_type == "TYPE_TEXT")
check("22: confidence parsed", abs(result.confidence - 0.9) < 0.001)

captured = {}

def _capture_generate(*, system_prompt, user_prompt, json_mode):
    captured["user_prompt"] = user_prompt
    return valid_raw

svc._llm.generate.side_effect = _capture_generate
svc.reason_next_step(
    goal="Search for restaurants",
    target_app="com.android.chrome",
    entities={"query": "restaurants"},
    screen_state=screen,
    step_history=[{"step_index": 1, "action_type": "TAP_ELEMENT", "params": {"selector": {"element_ref": "n5"}}, "result_success": False, "result_code": "NOT_FOUND"}],
    constraints={"max_steps_remaining": 9},
)
check("23: service injects failure context into prompt", "LAST ACTION FAILED:" in captured.get("user_prompt", ""))
check("24: service injects app context into prompt", "APP CONTEXT:" in captured.get("user_prompt", ""))

svc._llm.generate.side_effect = [LLMError("connection refused")]

# reset side effect cleanly by using a new service for failure scenarios
svc_fail = StepReasoningService()
svc_fail._llm = MagicMock()
svc_fail._llm.generate.side_effect = LLMError("connection refused")
wrong_app_screen = _make_screen([], pkg="com.android.launcher", title="Home")
fallback = svc_fail.reason_next_step(
    goal="Search for restaurants",
    target_app="com.android.chrome",
    entities={},
    screen_state=wrong_app_screen,
    step_history=[],
    constraints={"max_steps_remaining": 10},
)
check("25: executor recovery used when available", fallback.source == "executor_recovery")
check("26: recovery opens target app", fallback.action_type == "OPEN_APP" and fallback.params.get("package_name") == "com.android.chrome")

svc_manual = StepReasoningService()
svc_manual._llm = MagicMock()
svc_manual._llm.generate.return_value = {"action_type": "BLINK_ELEMENT"}
manual = svc_manual.reason_next_step(
    goal="Do something in an unsupported app",
    target_app="com.example.unsupported",
    entities={},
    screen_state=_make_screen([], pkg="com.example.unsupported", title="Example"),
    step_history=[],
    constraints={"max_steps_remaining": 10},
)
check("27: unsupported app failure becomes manual takeover", manual.fallback_mode == "manual_takeover")
check("28: manual takeover reason surfaced", manual.params.get("reason") == "llm_unavailable")

sensitive_service = StepReasoningService()
sensitive_service._llm = MagicMock()
sensitive_service._llm.generate.return_value = valid_raw
sensitive_result = sensitive_service.reason_next_step(
    goal="Search",
    target_app="com.android.chrome",
    entities={},
    screen_state=_make_screen([], is_sensitive=True),
    step_history=[],
    constraints={"max_steps_remaining": 5},
)
check("29: sensitive screen aborts before llm", sensitive_result.action_type == "ABORT")
check("30: sensitive screen reason preserved", sensitive_result.params.get("reason") == "sensitive_screen")


print("\n-- policy enforcement --")

policy_screen = _make_screen(
    [
        _node("send_btn", "android.widget.Button", text="Send", clickable=True),
        _node("safe_btn", "android.widget.Button", text="Open", clickable=True),
    ],
    pkg="com.whatsapp",
    title="WhatsApp",
)

tap_send = ReasonedStep(
    action_type="TAP_ELEMENT",
    params={"selector": {"element_ref": "send_btn"}},
    reasoning="Tap send.",
    confidence=0.8,
    is_goal_complete=False,
    requires_confirmation=False,
    sensitivity="low",
)
tap_policy = PolicyEnforcer.check_step(
    step=tap_send,
    session_goal="send a message",
    target_package="com.whatsapp",
    user_policy=None,
    step_count=1,
    screen_state=policy_screen,
)
check("31: send trigger requires confirmation", tap_policy.requires_confirmation)
check("32: send trigger escalates sensitivity", tap_policy.modified_sensitivity == "high")

type_sensitive = ReasonedStep(
    action_type="TYPE_TEXT",
    params={"text": "bank password 1234"},
    reasoning="Entering text.",
    confidence=0.7,
    is_goal_complete=False,
    requires_confirmation=False,
    sensitivity="medium",
)
type_policy = PolicyEnforcer.check_step(
    step=type_sensitive,
    session_goal="fill form",
    target_package="com.android.chrome",
    user_policy=None,
    step_count=1,
    screen_state=policy_screen,
)
check("33: blocked keyword stops text entry", not type_policy.allowed)
check("34: blocked reason set", type_policy.blocked_reason == "sensitive_content_detected")

no_text_policy = UserAutomationPolicy(user_id="u1", allow_text_entry=False)
user_block = PolicyEnforcer.check_step(
    step=ReasonedStep(
        action_type="TYPE_TEXT",
        params={"text": "hello"},
        reasoning="Typing hello.",
        confidence=0.8,
        is_goal_complete=False,
        requires_confirmation=False,
        sensitivity="low",
    ),
    session_goal="fill form",
    target_package="com.android.chrome",
    user_policy=no_text_policy,
    step_count=1,
    screen_state=policy_screen,
)
check("35: user policy can block text entry", not user_block.allowed)

hard_send_policy = UserAutomationPolicy(user_id="u2", require_hard_confirmation_for_send=True)
hard_confirm = PolicyEnforcer.check_step(
    step=ReasonedStep(
        action_type="TAP_ELEMENT",
        params={"selector": {"element_ref": "safe_btn"}},
        reasoning="Tap next.",
        confidence=0.8,
        is_goal_complete=False,
        requires_confirmation=False,
        sensitivity="low",
    ),
    session_goal="send the message",
    target_package="com.whatsapp",
    user_policy=hard_send_policy,
    step_count=1,
    screen_state=policy_screen,
)
check("36: hard send policy requires confirmation", hard_confirm.requires_confirmation)


print(f"\n{'-' * 50}")
passed = sum(results)
total = len(results)
print(f"  {passed}/{total} checks passed")
if passed < total:
    sys.exit(1)
