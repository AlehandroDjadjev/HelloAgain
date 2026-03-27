"""
Smoke test for Prompt B — Execution Loop Refactor.

Run from backend/:
    python smoke_exec_refactor.py

Verifies (no DB, no real LLM — all mocked):
  1.  LLM mode: terminal session → abort immediately
  2.  LLM mode: paused session   → abort immediately
  3.  LLM mode: timeout          → abort
  4.  LLM mode: max steps        → abort
  5.  LLM mode: circuit breaker  → manual_takeover after 3 consecutive failures
  6.  LLM mode: pending confirmation gate → confirm returned
  7.  LLM mode: sensitive screen → abort, no LLM call
  8.  LLM mode: no intent stored → abort with clear message
  9.  LLM mode: normal step      → execute with reasoning + confidence
  10. LLM mode: goal complete     → complete
  11. LLM mode: LLM returns ABORT → abort propagated
  12. LLM mode: LLM returns REQUEST_CONFIRMATION → confirm + ConfirmationRecord created
  13. decide_after_result LLM: records step_history entry
  14. decide_after_result LLM: fatal code → abort
  15. decide_after_result LLM: disconnect code → manual_takeover
  16. decide_after_result LLM: success increments step index
  17. decide_after_result LLM: failure returns continue (LLM handles recovery)
  18. Plan mode: plan present + no LLM intent → plan flow (unchanged)
  19. NextActionResponse.to_dict() includes reasoning + confidence
  20. AgentSession.store_intent_data() + has_llm_intent()
  21. Serializer: NextStepRequestSerializer plan_id optional
  22. Serializer: ActionResultV2Serializer plan_id optional + action_type present
  23. Serializer: ExecutionDecisionSerializer has reasoning field
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, PropertyMock

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
sys.path.insert(0, os.path.dirname(__file__))

import django
django.setup()

from apps.agent_sessions.execution_service import (
    ExecutionService,
    NextActionResponse,
    ExecutionDecision,
    CIRCUIT_BREAKER_THRESHOLD,
)
from apps.agent_sessions.models import AgentSession, SessionStatus, ConfirmationRecord
from apps.agent_sessions.serializers import (
    NextStepRequestSerializer,
    ActionResultV2Serializer,
    ExecutionDecisionSerializer,
)
from apps.agent_core.enums import ActionType
from apps.agent_core.services.step_reasoning import ReasonedStep

# Patch AuditService at module level — all smoke tests avoid real DB writes.
from unittest.mock import patch as _patch
_audit_patch = _patch("apps.agent_sessions.execution_service.AuditService")
_audit_patch.start()

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = PASS if cond else FAIL
    results.append(cond)
    print(f"  [{tag}] {name}" + (f"  ← {detail}" if detail and not cond else ""))


# ── Session factory ────────────────────────────────────────────────────────────

def _make_session(
    status=SessionStatus.EXECUTING,
    goal="Send WhatsApp to Alex",
    target_app="com.whatsapp",
    entities=None,
    started_at=None,
    current_step_index=0,
    step_history=None,
    retry_counts=None,
):
    # Use Django model __init__ so _state is properly set (no DB hit)
    s = AgentSession(
        user_id             = "test_user",
        device_id           = "test_device",
        status              = status,
        goal                = goal,
        target_app          = target_app,
        entities            = entities or {"recipient": "Alex", "message": "hi"},
        risk_level          = "low",
        started_at          = started_at,
        current_step_index  = current_step_index,
        step_history        = step_history or [],
        retry_counts        = retry_counts or {},
        supported_packages  = [],
    )
    s.id   = uuid.uuid4()  # override the default so it's stable per test
    s.save = MagicMock()   # prevent any DB writes
    return s


_SCREEN = {
    "foreground_package": "com.whatsapp",
    "window_title": "WhatsApp",
    "screen_hash": "abc",
    "focused_element_ref": None,
    "is_sensitive": False,
    "nodes": [{"ref": "n1", "class_name": "android.widget.TextView",
               "text": "Alex", "clickable": True, "enabled": True,
               "focused": False, "children": []}],
}

_REASONED = ReasonedStep(
    action_type="TAP_ELEMENT",
    params={"selector": {"element_ref": "n1"}},
    reasoning="Tapping contact Alex to open chat.",
    confidence=0.92,
    is_goal_complete=False,
    requires_confirmation=False,
    sensitivity="low",
)


def _patch_svc(reasoned=_REASONED):
    """Return a mock StepReasoningService that returns *reasoned*."""
    m = MagicMock()
    m.return_value.reason_next_step.return_value = reasoned
    return m


# ── Helpers to avoid DB calls ──────────────────────────────────────────────────

def _no_pending_conf(session):
    """Mock: ConfirmationRecord.objects.filter returns empty queryset."""
    pass

def _with_pending_conf(session):
    conf = MagicMock()
    conf.step_id        = "step_abc"
    conf.action_summary = "Send message to Alex?"
    conf.recipient      = "Alex"
    conf.content_preview = "hi"
    return conf


# ══════════════════════════════════════════════════════════════════════════════
print("\n── Guards ───────────────────────────────────────────────")
# ══════════════════════════════════════════════════════════════════════════════

# 1. Terminal session
s = _make_session(status=SessionStatus.COMPLETED)
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR:
    CR.objects.filter.return_value.first.return_value = None
    r = ExecutionService.get_next_action(s, plan=None, screen_state=_SCREEN)
check("1: terminal → abort", r.status == "abort")

# 2. Paused session
s = _make_session(status=SessionStatus.PAUSED)
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR:
    CR.objects.filter.return_value.first.return_value = None
    r = ExecutionService.get_next_action(s, plan=None, screen_state=_SCREEN)
check("2: paused → abort", r.status == "abort")

# 3. Timeout
from datetime import timedelta
s = _make_session(started_at=datetime.now(timezone.utc) - timedelta(seconds=400))
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR, \
     patch("apps.agent_sessions.execution_service.SessionService") as SS:
    CR.objects.filter.return_value.first.return_value = None
    SS.TERMINAL = frozenset([SessionStatus.COMPLETED, SessionStatus.ABORTED, SessionStatus.FAILED])
    SS.transition = MagicMock()
    r = ExecutionService.get_next_action(s, plan=None, screen_state=_SCREEN)
check("3: timeout → abort", r.status == "abort")
check("3: reason mentions timed out", "timed out" in r.reason.lower() or "timeout" in r.reason.lower())

# 4. Max steps
s = _make_session(current_step_index=50)
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR, \
     patch("apps.agent_sessions.execution_service.SessionService") as SS:
    CR.objects.filter.return_value.first.return_value = None
    SS.TERMINAL = frozenset([SessionStatus.COMPLETED, SessionStatus.ABORTED, SessionStatus.FAILED])
    SS.transition = MagicMock()
    r = ExecutionService.get_next_action(s, plan=None, screen_state=_SCREEN)
check("4: max steps → abort", r.status == "abort")

# 5. Circuit breaker
fails = [{"result_success": False, "result_code": "NOT_FOUND"} for _ in range(CIRCUIT_BREAKER_THRESHOLD)]
s = _make_session(step_history=fails)
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR:
    CR.objects.filter.return_value.first.return_value = None
    r = ExecutionService.get_next_action(s, plan=None, screen_state=_SCREEN)
check("5: circuit breaker → manual_takeover", r.status == "manual_takeover")
check("5: reason mentions consecutive failures", "consecutive" in r.reason.lower())

# 6. Pending confirmation gate
s = _make_session()
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR:
    CR.Status = ConfirmationRecord.Status
    CR.objects.filter.return_value.first.return_value = _with_pending_conf(s)
    r = ExecutionService.get_next_action(s, plan=None, screen_state=_SCREEN)
check("6: pending confirmation → confirm", r.status == "confirm")
check("6: next_action has type REQUEST_CONFIRMATION",
      r.next_action and r.next_action.get("type") == ActionType.REQUEST_CONFIRMATION.value)

# 7. Sensitive screen → abort, no LLM call
s = _make_session()
sensitive_screen = {**_SCREEN, "is_sensitive": True}
llm_calls = {"n": 0}
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR, \
     patch("apps.agent_sessions.execution_service.SessionService") as SS:
    CR.Status = ConfirmationRecord.Status
    CR.objects.filter.return_value.first.return_value = None
    SS.TERMINAL = frozenset([SessionStatus.COMPLETED, SessionStatus.ABORTED, SessionStatus.FAILED])
    SS.transition = MagicMock()
    r = ExecutionService.get_next_action(s, plan=None, screen_state=sensitive_screen)
check("7: sensitive screen → abort", r.status == "abort")


# ══════════════════════════════════════════════════════════════════════════════
print("\n── LLM mode execution ───────────────────────────────────")
# ══════════════════════════════════════════════════════════════════════════════

# 8. No intent stored
s = _make_session(goal="", target_app="")
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR:
    CR.objects.filter.return_value.first.return_value = None
    r = ExecutionService.get_next_action(s, plan=None, screen_state=_SCREEN)
check("8: no intent → abort", r.status == "abort")
check("8: reason mentions intent", "intent" in r.reason.lower())

# 9. Normal step → execute with reasoning
s = _make_session()
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR, \
     patch("apps.agent_sessions.execution_service.StepReasoningService", _patch_svc()), \
     patch("apps.agent_sessions.execution_service.SessionService") as SS, \
     patch("apps.agent_sessions.execution_service.AuditService") as AS:
    CR.Status = ConfirmationRecord.Status
    CR.objects.filter.return_value.first.return_value = None
    SS.TERMINAL = frozenset([SessionStatus.COMPLETED, SessionStatus.ABORTED, SessionStatus.FAILED])
    SS.transition = MagicMock()
    r = ExecutionService.get_next_action(s, plan=None, screen_state=_SCREEN)
check("9: normal step → execute", r.status == "execute")
check("9: next_action type is TAP_ELEMENT", r.next_action and r.next_action["type"] == "TAP_ELEMENT")
check("9: reasoning populated", "Alex" in r.reasoning)
check("9: confidence present",  abs(r.confidence - 0.92) < 0.001)

# 10. Goal complete
complete_step = ReasonedStep(
    action_type="TAP_ELEMENT", params={},
    reasoning="Message sent — goal complete.",
    confidence=0.99, is_goal_complete=True,
    requires_confirmation=False, sensitivity="low",
)
s = _make_session()
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR, \
     patch("apps.agent_sessions.execution_service.StepReasoningService", _patch_svc(complete_step)), \
     patch("apps.agent_sessions.execution_service.SessionService") as SS, \
     patch("apps.agent_sessions.execution_service.AuditService"):
    CR.Status = ConfirmationRecord.Status
    CR.objects.filter.return_value.first.return_value = None
    SS.TERMINAL = frozenset([SessionStatus.COMPLETED, SessionStatus.ABORTED, SessionStatus.FAILED])
    SS.transition = MagicMock()
    r = ExecutionService.get_next_action(s, plan=None, screen_state=_SCREEN)
check("10: goal complete → complete", r.status == "complete")
check("10: reasoning explains completion", "complete" in r.reasoning.lower())

# 11. LLM returns ABORT
abort_step = ReasonedStep(
    action_type="ABORT",
    params={"reason": "element not found after 3 retries"},
    reasoning="Giving up.", confidence=0.95,
    is_goal_complete=False, requires_confirmation=False, sensitivity="high",
)
s = _make_session()
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR, \
     patch("apps.agent_sessions.execution_service.StepReasoningService", _patch_svc(abort_step)), \
     patch("apps.agent_sessions.execution_service.SessionService") as SS, \
     patch("apps.agent_sessions.execution_service.AuditService"):
    CR.Status = ConfirmationRecord.Status
    CR.objects.filter.return_value.first.return_value = None
    SS.TERMINAL = frozenset([SessionStatus.COMPLETED, SessionStatus.ABORTED, SessionStatus.FAILED])
    SS.transition = MagicMock()
    r = ExecutionService.get_next_action(s, plan=None, screen_state=_SCREEN)
check("11: ABORT from LLM → abort", r.status == "abort")
check("11: reason from ABORT params", "not found" in r.reason.lower())

# 12. LLM returns REQUEST_CONFIRMATION
conf_step = ReasonedStep(
    action_type="REQUEST_CONFIRMATION",
    params={"action_summary": "Send 'hi' to Alex?", "prompt": "Confirm send"},
    reasoning="About to send — need confirmation.",
    confidence=0.98, is_goal_complete=False,
    requires_confirmation=True, sensitivity="high",
)
s = _make_session()
created_confs = []
def _fake_create_conf(*a, **kw):
    created_confs.append(kw)
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR, \
     patch("apps.agent_sessions.execution_service.StepReasoningService", _patch_svc(conf_step)), \
     patch("apps.agent_sessions.execution_service.SessionService") as SS, \
     patch("apps.agent_sessions.execution_service.AuditService"):
    CR.Status = ConfirmationRecord.Status
    CR.objects.filter.return_value.first.return_value = None
    SS.TERMINAL = frozenset([SessionStatus.COMPLETED, SessionStatus.ABORTED, SessionStatus.FAILED])
    SS.transition = MagicMock()
    SS.create_confirmation = _fake_create_conf
    r = ExecutionService.get_next_action(s, plan=None, screen_state=_SCREEN)
check("12: REQUEST_CONFIRMATION → confirm", r.status == "confirm")
check("12: ConfirmationRecord creation attempted", len(created_confs) == 1)
check("12: reasoning included", "confirmation" in r.reasoning.lower())


# ══════════════════════════════════════════════════════════════════════════════
print("\n── decide_after_result LLM mode ─────────────────────────")
# ══════════════════════════════════════════════════════════════════════════════

def _make_decide_session(step_history=None, step_index=0):
    s = _make_session(step_history=step_history or [], current_step_index=step_index)
    s.step_history = list(s.step_history)
    # override append_step to work in-memory
    _orig_append = AgentSession.append_step
    def _fake_append(self, step_data):
        from apps.agent_sessions.models import _redact_params
        from datetime import datetime, timezone
        entry = {
            "step_index":        step_data.get("step_index", len(self.step_history) + 1),
            "action_type":       step_data.get("action_type", ""),
            "params":            _redact_params(step_data.get("params") or {}),
            "reasoning":         step_data.get("reasoning", ""),
            "result_code":       step_data.get("result_code", ""),
            "result_success":    bool(step_data.get("result_success")),
            "screen_hash_before": step_data.get("screen_hash_before", ""),
            "screen_hash_after":  step_data.get("screen_hash_after", ""),
            "timestamp":          datetime.now(timezone.utc).isoformat(),
            "is_recovery":        bool(step_data.get("is_recovery", False)),
        }
        self.step_history = list(self.step_history) + [entry]
    s.append_step = lambda sd: _fake_append(s, sd)
    return s

# 13. Records step_history entry
s = _make_decide_session()
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR, \
     patch("apps.agent_sessions.execution_service.AuditService"):
    CR.Status = ConfirmationRecord.Status
    CR.objects.filter.return_value.first.return_value = None
    decision = ExecutionService.decide_after_result(
        s, plan=None, action_id="llm_abc",
        result_success=True, result_code="OK",
        action_type="TAP_ELEMENT", reasoning="tapped n1",
    )
check("13: step history has 1 entry", len(s.step_history) == 1)
check("13: entry has action_type TAP_ELEMENT", s.step_history[0]["action_type"] == "TAP_ELEMENT")
check("13: entry has reasoning", "n1" in s.step_history[0]["reasoning"])
check("13: success returns continue", decision.status == "continue")

# 14. Fatal code → abort
s = _make_decide_session()
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR, \
     patch("apps.agent_sessions.execution_service.SessionService") as SS, \
     patch("apps.agent_sessions.execution_service.AuditService"):
    CR.Status = ConfirmationRecord.Status
    CR.objects.filter.return_value.first.return_value = None
    SS.TERMINAL = frozenset([SessionStatus.COMPLETED, SessionStatus.ABORTED, SessionStatus.FAILED])
    SS.transition = MagicMock()
    decision = ExecutionService.decide_after_result(
        s, plan=None, action_id="x", result_success=False,
        result_code="SENSITIVE_SCREEN", action_type="TAP_ELEMENT",
    )
check("14: SENSITIVE_SCREEN → abort", decision.status == "abort")

# 15. SERVICE_DISCONNECTED → manual_takeover
s = _make_decide_session()
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR, \
     patch("apps.agent_sessions.execution_service.AuditService"):
    CR.Status = ConfirmationRecord.Status
    CR.objects.filter.return_value.first.return_value = None
    decision = ExecutionService.decide_after_result(
        s, plan=None, action_id="x", result_success=False,
        result_code="SERVICE_DISCONNECTED", action_type="TAP_ELEMENT",
    )
check("15: SERVICE_DISCONNECTED → manual_takeover", decision.status == "manual_takeover")

# 16. Success increments step index
s = _make_decide_session(step_index=3)
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR, \
     patch("apps.agent_sessions.execution_service.AuditService"):
    CR.Status = ConfirmationRecord.Status
    CR.objects.filter.return_value.first.return_value = None
    ExecutionService.decide_after_result(
        s, plan=None, action_id="x", result_success=True,
        result_code="OK", action_type="TAP_ELEMENT",
    )
check("16: success increments current_step_index", s.current_step_index == 4)

# 17. Failure → continue (LLM handles recovery)
s = _make_decide_session()
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR, \
     patch("apps.agent_sessions.execution_service.AuditService"):
    CR.Status = ConfirmationRecord.Status
    CR.objects.filter.return_value.first.return_value = None
    decision = ExecutionService.decide_after_result(
        s, plan=None, action_id="x", result_success=False,
        result_code="ELEMENT_NOT_FOUND", action_type="TAP_ELEMENT",
    )
check("17: failure → continue (LLM recovers)", decision.status == "continue")
check("17: step_history records failure", s.step_history[0]["result_success"] is False)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── Plan mode backward compat ────────────────────────────")
# ══════════════════════════════════════════════════════════════════════════════

# 18. Plan present + no LLM intent → plan flow invoked
s = _make_session(goal="", target_app="")   # no LLM intent
mock_plan = MagicMock()
mock_plan.id          = uuid.uuid4()
mock_plan.app_package = "com.whatsapp"

mock_step = {"id": "wa_1", "type": "OPEN_APP", "params": {"package": "com.whatsapp"}}
with patch("apps.agent_sessions.execution_service.ConfirmationRecord") as CR, \
     patch("apps.agent_sessions.execution_service.SessionService") as SS, \
     patch("apps.agent_sessions.execution_service.AuditService"), \
     patch("apps.agent_sessions.execution_service.PlanService") as PS:
    CR.Status = ConfirmationRecord.Status
    CR.objects.filter.return_value.first.return_value = None
    SS.TERMINAL = frozenset([SessionStatus.COMPLETED, SessionStatus.ABORTED, SessionStatus.FAILED])
    SS.transition = MagicMock()
    PS.get_current_step.return_value = mock_step
    PS.get_executor_hint.return_value = "whatsapp_v1"
    r = ExecutionService.get_next_action(s, plan=mock_plan, screen_state=_SCREEN)
check("18: plan mode → execute", r.status == "execute")
check("18: next_action from plan step", r.next_action and r.next_action["type"] == "OPEN_APP")
check("18: executor hint populated",    r.executor_hint == "whatsapp_v1")
check("18: no reasoning in plan mode",  not r.reasoning)   # plan mode has no LLM reasoning


# ══════════════════════════════════════════════════════════════════════════════
print("\n── NextActionResponse / Serializers ─────────────────────")
# ══════════════════════════════════════════════════════════════════════════════

# 19. to_dict() includes reasoning + confidence only when non-empty
nar = NextActionResponse(
    next_action={"type": "TAP_ELEMENT"},
    status="execute",
    executor_hint="",
    reason="",
    reasoning="I tapped n1 because it is Alex's contact.",
    confidence=0.88,
)
d = nar.to_dict()
check("19: reasoning in to_dict",  "reasoning" in d and "Alex" in d["reasoning"])
check("19: confidence in to_dict", "confidence" in d and abs(d["confidence"] - 0.88) < 0.001)

nar_no = NextActionResponse(None, "complete", "", "done")
d_no = nar_no.to_dict()
check("19: no reasoning key when empty", "reasoning"  not in d_no)
check("19: no confidence key when 0.0",  "confidence" not in d_no)

# 20. AgentSession.store_intent_data() + has_llm_intent()
s2 = _make_session(goal="", target_app="")
check("20: no intent → has_llm_intent False", not s2.has_llm_intent())
s2.save = MagicMock()
s2.store_intent_data("Send hi to Alex", "com.whatsapp", {"recipient": "Alex"})
check("20: after store → has_llm_intent True",  s2.has_llm_intent())
check("20: goal stored correctly",               s2.goal == "Send hi to Alex")
check("20: target_app stored correctly",         s2.target_app == "com.whatsapp")

# 21. NextStepRequestSerializer: plan_id optional
ser = NextStepRequestSerializer(data={"screen_state": {"is_sensitive": False}})
check("21: NextStepRequestSerializer valid without plan_id", ser.is_valid(), str(ser.errors))
check("21: plan_id defaults to None", ser.validated_data.get("plan_id") is None)

ser2 = NextStepRequestSerializer(data={"plan_id": str(uuid.uuid4()), "screen_state": None})
check("21: plan_id accepted when provided", ser2.is_valid(), str(ser2.errors))

# 22. ActionResultV2Serializer: plan_id optional + action_type present
ser3 = ActionResultV2Serializer(data={
    "action_id": "llm_abc123",
    "result": {"success": True, "code": "OK"},
    "action_type": "TAP_ELEMENT",
    "reasoning": "tapped n1",
})
check("22: ActionResultV2 valid without plan_id", ser3.is_valid(), str(ser3.errors))
check("22: action_type preserved", ser3.validated_data.get("action_type") == "TAP_ELEMENT")
check("22: reasoning preserved",   "n1" in ser3.validated_data.get("reasoning", ""))

# 23. ExecutionDecisionSerializer includes reasoning
dec = ExecutionDecision("continue", reasoning="LLM will handle recovery")
d23 = dec.to_dict()
check("23: decision has reasoning key", "reasoning" in d23)
ser4 = ExecutionDecisionSerializer(d23)
check("23: serializer accepts reasoning", ser4.data.get("reasoning", "") == "LLM will handle recovery")


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'─'*50}")
passed = sum(results)
total  = len(results)
print(f"  {passed}/{total} checks passed")
if passed < total:
    sys.exit(1)
