"""
Smoke test — Stage 8: Fixed plan runner and execution loop.

Tests:
  1. Session created and transitioned to APPROVED correctly
  2. get_next_action() transitions APPROVED → EXECUTING on first call
  3. Each step is returned in order with status="execute"
  4. REQUEST_CONFIRMATION step returns status="confirm" and creates ConfirmationRecord
  5. After posting success result for confirmation, the next step is returned
  6. Session completes after final step
  7. Retry counting: retryable error increments counter; at max_attempts → abort
  8. Fatal error code → abort immediately
  9. Session-level timeout check
 10. Max step count guard
 11. Sensitive screen state → abort
 12. Foreground package mismatch → retry, then manual_takeover after MAX_SCREEN_RETRIES
 13. run_test_plan management command --auto flag

Run:
    python smoke_stage8.py
"""
import os
import sys
import uuid
import json

# Force keyword-fallback for intent parsing (avoid loading Qwen)
os.environ.setdefault("LLM_PROVIDER",  "ollama")
os.environ.setdefault("LLM_TIMEOUT",   "1")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

import django
django.setup()

from datetime import datetime, timezone, timedelta
from apps.agent_sessions.models import AgentSession, SessionStatus, ConfirmationRecord
from apps.agent_sessions.services import SessionService
from apps.agent_sessions.execution_service import (
    ExecutionService, MAX_SCREEN_RETRIES, SESSION_TIMEOUT_SECONDS,
    MAX_STEPS_PER_SESSION,
)
from apps.agent_plans.models import ActionPlanRecord, PlanStatus
from apps.agent_plans.services import PlanService
from apps.agent_core.schemas import ActionPlan as ActionPlanSchema
from apps.device_bridge.services import DeviceBridgeService
from apps.agent_core.enums import ActionResultStatus

# ── Minimal plan for tests ────────────────────────────────────────────────────

BASE_PLAN = {
    "goal":        "Test plan",
    "app_package": "com.whatsapp",
    "steps": [
        {
            "id": "s1", "type": "OPEN_APP",
            "params": {"package": "com.whatsapp"},
            "expected_outcome": {"screen_hint": "chat_list"},
            "timeout_ms": 5000, "retry_policy": {"max_attempts": 2},
            "sensitivity": "low", "requires_confirmation": False,
        },
        {
            "id": "s2", "type": "TAP_ELEMENT",
            "params": {"selector": {"content_desc": "Search"}},
            "expected_outcome": {"screen_hint": "search_open"},
            "timeout_ms": 4000, "retry_policy": {"max_attempts": 2},
            "sensitivity": "low", "requires_confirmation": False,
        },
        {
            "id": "s3", "type": "REQUEST_CONFIRMATION",
            "params": {
                "action_summary": "Send test message",
                "recipient": "TestUser",
                "content_preview": "Hello",
            },
            "expected_outcome": {"screen_hint": "confirmation_shown"},
            "timeout_ms": 60000, "retry_policy": {"max_attempts": 1},
            "sensitivity": "medium", "requires_confirmation": False,
        },
        {
            "id": "s4", "type": "TAP_ELEMENT",
            "params": {"selector": {"content_desc": "Send"}},
            "expected_outcome": {"screen_hint": "message_sent"},
            "timeout_ms": 5000, "retry_policy": {"max_attempts": 2},
            "sensitivity": "high", "requires_confirmation": True,
        },
    ],
}

NORMAL_SCREEN = {
    "foreground_package": "com.whatsapp",
    "screen_hash": "abc",
    "is_sensitive": False,
    "nodes": [],
}

SENSITIVE_SCREEN = {**NORMAL_SCREEN, "is_sensitive": True}
WRONG_APP_SCREEN = {**NORMAL_SCREEN, "foreground_package": "com.evil.app"}


def _make_session_and_plan(steps_override=None):
    session = SessionService.create(user_id="smoketest", device_id="test")
    steps   = steps_override or BASE_PLAN["steps"]

    PlanService.store_intent(session=session, raw_transcript="test", parsed_intent={})
    validated = ActionPlanSchema.model_validate({
        "plan_id":     uuid.uuid4().hex,
        "session_id":  str(session.id),
        "goal":        "test",
        "app_package": "com.whatsapp",
        "steps":       steps,
    })
    plan = PlanService.store_plan(session, validated)
    plan.status = PlanStatus.APPROVED
    plan.save(update_fields=["status"])
    SessionService.transition(session, SessionStatus.APPROVED)
    return session, plan


def _succeed_step(session, plan, step_id, step_type=""):
    DeviceBridgeService.record_action_result(
        session=session, plan_id=plan.id,
        step_id=step_id, step_type=step_type,
        status=ActionResultStatus.SUCCESS.value,
        executed_at=datetime.now(timezone.utc), duration_ms=10,
    )
    session.refresh_from_db()


PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
results = []


def check(name, condition, detail=""):
    status = PASS if condition else FAIL
    print(f"  {status} {name}" + (f" — {detail}" if detail else ""))
    results.append(condition)


# ── Test 1: APPROVED → EXECUTING on first get_next_action ────────────────────

print("\nTest 1: First call transitions APPROVED → EXECUTING")
s, p = _make_session_and_plan()
resp = ExecutionService.get_next_action(s, p)
s.refresh_from_db()
check("status == execute",  resp.status == "execute")
check("session EXECUTING",  s.status == SessionStatus.EXECUTING)
check("next_action is s1",  resp.next_action and resp.next_action["id"] == "s1")
check("started_at set",     s.started_at is not None)

# ── Test 2: Steps returned in sequence ───────────────────────────────────────

print("\nTest 2: Steps 1-2 returned in sequence")
_succeed_step(s, p, "s1")
resp2 = ExecutionService.get_next_action(s, p, screen_state=NORMAL_SCREEN)
check("next step is s2",    resp2.next_action and resp2.next_action["id"] == "s2")
check("status == execute",  resp2.status == "execute")

# ── Test 3: REQUEST_CONFIRMATION step ────────────────────────────────────────

print("\nTest 3: REQUEST_CONFIRMATION → confirm")
_succeed_step(s, p, "s2")
s.refresh_from_db()
resp3 = ExecutionService.get_next_action(s, p)
check("status == confirm",         resp3.status == "confirm")
check("ConfirmationRecord created",
      ConfirmationRecord.objects.filter(session=s, step_id="s3").exists())

# Approve confirmation then advance
conf = ConfirmationRecord.objects.get(session=s, step_id="s3")
conf.status = ConfirmationRecord.Status.APPROVED
conf.save()
_succeed_step(s, p, "s3")
s.refresh_from_db()
resp4 = ExecutionService.get_next_action(s, p)
check("next step after confirm is s4", resp4.next_action and resp4.next_action["id"] == "s4")
check("status == execute", resp4.status == "execute")

# ── Test 4: Session complete ──────────────────────────────────────────────────

print("\nTest 4: All steps done → complete")
_succeed_step(s, p, "s4")
resp_done = ExecutionService.get_next_action(s, p)
check("status == complete", resp_done.status == "complete")
s.refresh_from_db()
check("session COMPLETED", s.status == SessionStatus.COMPLETED)

# ── Test 5: Retry counting ────────────────────────────────────────────────────

print("\nTest 5: Retry counting — ELEMENT_NOT_FOUND")
s5, p5 = _make_session_and_plan()
ExecutionService.get_next_action(s5, p5)  # transition to EXECUTING
s5.refresh_from_db()

d1 = ExecutionService.decide_after_result(s5, p5, "s1", False, "ELEMENT_NOT_FOUND")
check("first failure → retry", d1.status == "retry")
s5.refresh_from_db()
check("retry_counts incremented", s5.retry_counts.get("s1", 0) == 1)

d2 = ExecutionService.decide_after_result(s5, p5, "s1", False, "ELEMENT_NOT_FOUND")
check("second failure → retry", d2.status == "retry")

d3 = ExecutionService.decide_after_result(s5, p5, "s1", False, "ELEMENT_NOT_FOUND")
check("third failure → abort (max_attempts=2)", d3.status == "abort")

# ── Test 6: Fatal error → immediate abort ────────────────────────────────────

print("\nTest 6: Fatal error code → abort")
s6, p6 = _make_session_and_plan()
ExecutionService.get_next_action(s6, p6)
s6.refresh_from_db()
d = ExecutionService.decide_after_result(s6, p6, "s1", False, "SENSITIVE_SCREEN")
check("SENSITIVE_SCREEN → abort", d.status == "abort")

# ── Test 7: Sensitive screen state ───────────────────────────────────────────

print("\nTest 7: Sensitive screen in get_next_action → abort")
s7, p7 = _make_session_and_plan()
ExecutionService.get_next_action(s7, p7)  # APPROVED → EXECUTING
s7.refresh_from_db()
resp7 = ExecutionService.get_next_action(s7, p7, screen_state=SENSITIVE_SCREEN)
check("status == abort", resp7.status == "abort")
check("reason mentions sensitive", "sensitive" in resp7.reason.lower())

# ── Test 8: Foreground mismatch → retry then manual_takeover ─────────────────
# s1 is OPEN_APP (skips app check) — advance past it, then test s2 (TAP_ELEMENT)

print(f"\nTest 8: Foreground mismatch — {MAX_SCREEN_RETRIES} retries → manual_takeover")
s8, p8 = _make_session_and_plan()
ExecutionService.get_next_action(s8, p8)          # APPROVED → EXECUTING
s8.refresh_from_db()
_succeed_step(s8, p8, "s1", "OPEN_APP")           # advance past OPEN_APP (skips check)
s8.refresh_from_db()

statuses = []
for _ in range(MAX_SCREEN_RETRIES + 2):
    s8.refresh_from_db()
    resp8 = ExecutionService.get_next_action(s8, p8, screen_state=WRONG_APP_SCREEN)
    statuses.append(resp8.status)

check(f"first {MAX_SCREEN_RETRIES} are retry",
      all(s == "retry" for s in statuses[:MAX_SCREEN_RETRIES]))
check("eventually escalates to manual_takeover",
      "manual_takeover" in statuses)

# ── Test 9: Session timeout ───────────────────────────────────────────────────

print("\nTest 9: Session timeout")
s9, p9 = _make_session_and_plan()
ExecutionService.get_next_action(s9, p9)
s9.refresh_from_db()
# Backdate started_at
s9.started_at = datetime.now(timezone.utc) - timedelta(seconds=SESSION_TIMEOUT_SECONDS + 10)
s9.status = SessionStatus.EXECUTING
s9.save(update_fields=["started_at", "status", "updated_at"])

resp9 = ExecutionService.get_next_action(s9, p9)
check("status == abort", resp9.status == "abort")
check("timeout in reason", "timed out" in resp9.reason.lower() or "timeout" in resp9.reason.lower())

# ── Test 10: Max step count ───────────────────────────────────────────────────

print("\nTest 10: Max step count guard")
s10, p10 = _make_session_and_plan()
s10.status = SessionStatus.EXECUTING
s10.started_at = datetime.now(timezone.utc)
s10.current_step_index = MAX_STEPS_PER_SESSION
s10.save(update_fields=["status", "started_at", "current_step_index", "updated_at"])

resp10 = ExecutionService.get_next_action(s10, p10)
check("status == abort", resp10.status == "abort")
check("max_steps in reason", "step" in resp10.reason.lower())

# ── Test 11: run_test_plan --auto ─────────────────────────────────────────────

print("\nTest 11: run_test_plan management command --auto")
import subprocess, sys
result = subprocess.run(
    [sys.executable, "manage.py", "run_test_plan",
     "--plan-file", "whatsapp_test.json", "--auto"],
    capture_output=True, text=True, encoding="utf-8",
)
check("exit code 0",             result.returncode == 0,
      detail=result.stderr[:200] if result.returncode != 0 else "")
check("COMPLETE in output",      "COMPLETE" in result.stdout,
      detail=result.stdout[-200:] if "COMPLETE" not in result.stdout else "")

# ── Summary ───────────────────────────────────────────────────────────────────

passed = sum(results)
total  = len(results)
print(f"\n{'='*55}")
print(f"Stage 8 smoke test: {passed}/{total} checks passed")
if passed < total:
    print("\033[91mSome checks FAILED — see output above.\033[0m")
    sys.exit(1)
else:
    print("\033[92mAll checks passed.\033[0m")
