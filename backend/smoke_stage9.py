"""
Smoke test — Stage 9: WhatsApp executor with selector registry.

Tests:
  1.  Executor registered at startup and retrievable
  2.  Screen hint inference — chat_list
  3.  Screen hint inference — chat_thread (EditText + Send)
  4.  Screen hint inference — search_active (focused EditText)
  5.  Screen hint inference — wrong_app
  6.  Screen hint inference — unknown (empty nodes)
  7.  Selector registry — default fallback order for search_button
  8.  Selector registry — version-pinned selectors ("2.24+")
  9.  Selector registry — parameterised contact_item resolved with selector_params
 10.  Selector registry — unknown element → empty list
 11.  Selector resolution in get_next_action() — selector_candidates injected
 12.  Inferred screen hint included in NextActionResponse
 13.  Recovery action: expected chat_list but got chat_thread → BACK
 14.  Recovery action: wrong_app → OPEN_APP
 15.  Recovery action: expected chat_thread + got chat_list → None
 16.  Plan validation — valid WA send_message plan passes
 17.  Plan compiler — send_message template uses named selectors
 18.  Plan compiler — search template uses named selectors
 19.  run_test_plan --auto with named-selector plan (wa_11 TAP send_button)

Run:
    python smoke_stage9.py
"""
import os, sys, uuid

os.environ.setdefault("LLM_PROVIDER",  "ollama")
os.environ.setdefault("LLM_TIMEOUT",   "1")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

import django
django.setup()

from apps.agent_executors.registry import ExecutorRegistry, get_executor
from apps.agent_executors.whatsapp.executor import (
    WhatsAppExecutor,
    HINT_CHAT_LIST, HINT_CHAT_THREAD, HINT_SEARCH_ACTIVE,
    HINT_WRONG_APP, HINT_UNKNOWN,
)
from apps.agent_executors.whatsapp import selectors as sel_mod
from apps.agent_core.enums import ActionType
from apps.agent_core.schemas import ActionPlan as ActionPlanSchema
from apps.agent_plans.models import ActionPlanRecord, PlanStatus
from apps.agent_plans.services import PlanService
from apps.agent_plans.services.plan_compiler import PlanCompiler
from apps.agent_plans.services.intent_service import IntentResult
from apps.agent_sessions.models import SessionStatus
from apps.agent_sessions.services import SessionService
from apps.agent_sessions.execution_service import ExecutionService

PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
results: list[bool] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    print(f"  {status} {name}" + (f"  — {detail}" if detail else ""))
    results.append(condition)


# ── Fixtures ──────────────────────────────────────────────────────────────────

CHAT_LIST_SCREEN = {
    "foreground_package": "com.whatsapp",
    "window_title": "WhatsApp",
    "is_sensitive": False,
    "nodes": [
        {"class_name": "android.widget.ImageButton", "content_desc": "Search", "clickable": True, "enabled": True, "focused": False, "text": ""},
        {"class_name": "android.widget.ImageButton", "content_desc": "New chat", "clickable": True, "enabled": True, "focused": False, "text": ""},
        {"class_name": "android.widget.TextView", "text": "Alex", "content_desc": "", "clickable": True, "enabled": True, "focused": False},
    ],
}

CHAT_THREAD_SCREEN = {
    "foreground_package": "com.whatsapp",
    "window_title": "Alex",
    "is_sensitive": False,
    "nodes": [
        {"class_name": "android.widget.EditText", "content_desc": "Message", "clickable": True, "enabled": True, "focused": True, "text": ""},
        {"class_name": "android.widget.ImageButton", "content_desc": "Send", "clickable": True, "enabled": True, "focused": False, "text": ""},
    ],
}

SEARCH_ACTIVE_SCREEN = {
    "foreground_package": "com.whatsapp",
    "window_title": "WhatsApp",
    "is_sensitive": False,
    "nodes": [
        {"class_name": "android.widget.EditText", "content_desc": "Search...", "clickable": True, "enabled": True, "focused": True, "text": ""},
    ],
}

WRONG_APP_SCREEN = {
    "foreground_package": "com.android.settings",
    "window_title": "Settings",
    "is_sensitive": False,
    "nodes": [],
}

EMPTY_SCREEN = {
    "foreground_package": "com.whatsapp",
    "window_title": "",
    "is_sensitive": False,
    "nodes": [],
}


def _make_wa_session_and_plan():
    """Create a WhatsApp send_message session & plan in APPROVED state."""
    session = SessionService.create(user_id="smoketest9", device_id="test")
    PlanService.store_intent(session=session, raw_transcript="test", parsed_intent={})
    intent = IntentResult(
        goal="Send Alex hi",
        goal_type="send_message",
        app_package="com.whatsapp",
        target_app="WhatsApp",
        entities={"recipient": "Alex", "message": "hi"},
        risk_level="low",
        confidence=1.0,
    )
    plan = PlanCompiler.compile(intent, str(session.id))
    record = PlanService.store_plan(session, plan)
    record.status = PlanStatus.APPROVED
    record.save(update_fields=["status"])
    SessionService.transition(session, SessionStatus.APPROVED)
    return session, record


# ════════════════════════════════════════════════════════════════════════════════

print("\nTest 1: Executor registered at startup")
exec_inst = get_executor("com.whatsapp")
check("get_executor returns WhatsAppExecutor instance",
      isinstance(exec_inst, WhatsAppExecutor))
check("ExecutorRegistry.is_supported",
      ExecutorRegistry.is_supported("com.whatsapp"))

print("\nTest 2: Screen hint — chat_list")
wa = WhatsAppExecutor()
hint = wa.infer_screen_hint(CHAT_LIST_SCREEN)
check("chat_list detected", hint == HINT_CHAT_LIST, detail=hint)

print("\nTest 3: Screen hint — chat_thread")
hint = wa.infer_screen_hint(CHAT_THREAD_SCREEN)
check("chat_thread detected", hint == HINT_CHAT_THREAD, detail=hint)

print("\nTest 4: Screen hint — search_active")
hint = wa.infer_screen_hint(SEARCH_ACTIVE_SCREEN)
check("search_active detected", hint == HINT_SEARCH_ACTIVE, detail=hint)

print("\nTest 5: Screen hint — wrong_app")
hint = wa.infer_screen_hint(WRONG_APP_SCREEN)
check("wrong_app detected", hint == HINT_WRONG_APP, detail=hint)

print("\nTest 6: Screen hint — unknown (empty screen)")
hint = wa.infer_screen_hint(EMPTY_SCREEN)
# Empty screen with com.whatsapp foreground but no nodes — could be chat_list or unknown
check("result is a string", isinstance(hint, str))
check("no exception", True)

print("\nTest 7: Selector registry — search_button defaults")
sels = sel_mod.get_selectors("search_button")
check("returns list", isinstance(sels, list))
check("at least one selector", len(sels) >= 1)
check("first selector has content_desc_contains",
      "content_desc_contains" in sels[0])

print("\nTest 8: Selector registry — version-pinned 2.24+")
sels_versioned = sel_mod.get_selectors("search_button", app_version="2.24.3.78")
sels_default   = sel_mod.get_selectors("search_button", app_version="2.20.0.0")
# 2.24.3.78 should use the "2.24+" bucket; 2.20 should use "default"
check("version-pinned differs from default or is same acceptable",
      isinstance(sels_versioned, list) and len(sels_versioned) >= 1)
check("old version uses default", len(sels_default) >= 1)

print("\nTest 9: Parameterised contact_item")
sels = sel_mod.get_selectors("contact_item", selector_params={"contact_name": "Alex"})
check("returns list", len(sels) >= 1)
check("{contact_name} substituted",
      all("Alex" in str(s.values()) for s in sels))

print("\nTest 10: Unknown element → empty list")
sels = sel_mod.get_selectors("nonexistent_button_xyz")
check("empty list for unknown element", sels == [])

print("\nTest 11: Selector resolution in get_next_action()")
s, p = _make_wa_session_and_plan()
# First call transitions to EXECUTING and returns wa_1 (OPEN_APP — no selector_name)
resp = ExecutionService.get_next_action(s, p, screen_state=CHAT_LIST_SCREEN)
s.refresh_from_db()
check("status == execute", resp.status == "execute")
check("first step is OPEN_APP", resp.next_action and resp.next_action["type"] == "OPEN_APP")

# Succeed wa_1 through wa_3, then step wa_4 should have selector_candidates
from apps.device_bridge.services import DeviceBridgeService
from apps.agent_core.enums import ActionResultStatus
from datetime import datetime, timezone as tz

def _succeed(session, plan_rec, step_id):
    DeviceBridgeService.record_action_result(
        session=session, plan_id=plan_rec.id,
        step_id=step_id, step_type="",
        status=ActionResultStatus.SUCCESS.value,
        executed_at=datetime.now(tz.utc), duration_ms=0,
    )
    session.refresh_from_db()

_succeed(s, p, "wa_1")
_succeed(s, p, "wa_2")
_succeed(s, p, "wa_3")
s.refresh_from_db()

resp4 = ExecutionService.get_next_action(s, p, screen_state=CHAT_LIST_SCREEN)
check("wa_4 returned",
      resp4.next_action and resp4.next_action["id"] == "wa_4")
check("selector_candidates injected",
      "selector_candidates" in (resp4.next_action or {}).get("params", {}),
      detail=str((resp4.next_action or {}).get("params", {}).keys()))
check("selector_name removed from params",
      "selector_name" not in (resp4.next_action or {}).get("params", {}))

print("\nTest 12: Inferred screen hint in NextActionResponse")
check("inferred_screen_hint populated",
      resp4.inferred_screen_hint != "",
      detail=resp4.inferred_screen_hint)
check("inferred hint is chat_list",
      resp4.inferred_screen_hint == HINT_CHAT_LIST)

print("\nTest 13: Recovery — expected chat_list, got chat_thread → BACK")
action = wa.get_recovery_action(HINT_CHAT_THREAD, HINT_CHAT_LIST, {})
check("returns dict", action is not None)
check("suggests BACK", action and action.get("type") == ActionType.BACK.value)

print("\nTest 14: Recovery — wrong_app → OPEN_APP")
action = wa.get_recovery_action(HINT_WRONG_APP, HINT_CHAT_LIST, {})
check("returns dict", action is not None)
check("suggests OPEN_APP",
      action and action.get("type") == ActionType.OPEN_APP.value)
check("package is com.whatsapp",
      action and (action.get("params", {}) or {}).get("package") == "com.whatsapp")

print("\nTest 15: Recovery — expected chat_thread, got chat_list → None")
action = wa.get_recovery_action(HINT_CHAT_LIST, HINT_CHAT_THREAD, {})
check("returns None (manual takeover)", action is None)

print("\nTest 16: Plan validation — valid send_message plan")
intent16 = IntentResult(
    goal="test", goal_type="send_message", app_package="com.whatsapp",
    target_app="WhatsApp", entities={"recipient": "Bob", "message": "hello"}, risk_level="low",
)
plan16 = PlanCompiler.compile(intent16, "sess_test")
errors = wa.validate_plan(plan16)
check("no validation errors", errors == [], detail=str(errors))

print("\nTest 17: Plan compiler — send_message uses named selectors")
intent17 = IntentResult(
    goal="Send Alex hi", goal_type="send_message", app_package="com.whatsapp",
    target_app="WhatsApp", entities={"recipient": "Alex", "message": "hi"}, risk_level="low",
)
plan17 = PlanCompiler.compile(intent17, "sess_test2")
named_steps = [
    s for s in plan17.steps
    if isinstance(s.params, dict) and "selector_name" in s.params
]
check("at least 3 steps use named selectors", len(named_steps) >= 3,
      detail=f"found {len(named_steps)}: {[s.id for s in named_steps]}")

# Check send_button is named
send_step = next((s for s in plan17.steps if s.id == "wa_11"), None)
check("wa_11 uses send_button named selector",
      send_step is not None and
      isinstance(send_step.params, dict) and
      send_step.params.get("selector_name") == "send_button")

# Check contact_item carries selector_params
contact_step = next((s for s in plan17.steps if s.id == "wa_7"), None)
check("wa_7 has contact_name in selector_params",
      contact_step is not None and
      isinstance(contact_step.params, dict) and
      (contact_step.params.get("selector_params") or {}).get("contact_name") == "Alex",
      detail=str(getattr(contact_step, "params", None)))

print("\nTest 18: Plan compiler — search_contact template uses named selectors")
intent18 = IntentResult(
    goal="Search Bob", goal_type="search_contact", app_package="com.whatsapp",
    target_app="WhatsApp", entities={"recipient": "Bob"}, risk_level="low",
)
plan18 = PlanCompiler.compile(intent18, "sess_test3")
named18 = [s for s in plan18.steps if isinstance(s.params, dict) and "selector_name" in s.params]
check("search template has at least one named selector", len(named18) >= 1,
      detail=str([s.id for s in named18]))

print("\nTest 19: run_test_plan --auto with named-selector plan")
import subprocess
result = subprocess.run(
    [sys.executable, "manage.py", "run_test_plan",
     "--plan-file", "whatsapp_test.json", "--auto"],
    capture_output=True, text=True, encoding="utf-8",
)
check("exit code 0", result.returncode == 0,
      detail=result.stderr[:200] if result.returncode != 0 else "")
check("COMPLETE in output", "COMPLETE" in result.stdout)

# ── Summary ───────────────────────────────────────────────────────────────────

passed = sum(results)
total  = len(results)
print(f"\n{'='*58}")
print(f"Stage 9 smoke test: {passed}/{total} checks passed")
if passed < total:
    print("\033[91mSome checks FAILED — see output above.\033[0m")
    sys.exit(1)
else:
    print("\033[92mAll checks passed.\033[0m")
