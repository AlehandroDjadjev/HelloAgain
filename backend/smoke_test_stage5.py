"""
Stage 5 smoke test — exercises every new REST endpoint end-to-end.

Run with: python smoke_test_stage5.py
"""
import os, sys, django, uuid, json
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
sys.path.insert(0, os.path.dirname(__file__))
django.setup()

from django.test import RequestFactory
from rest_framework.test import APIRequestFactory
from apps.agent_sessions.models import AgentSession, SessionStatus, ConfirmationRecord
from apps.agent_plans.models import ActionPlanRecord, PlanStatus
from apps.agent_sessions.services import SessionService
from apps.agent_plans.services import IntentService, PlanService
from apps.agent_sessions.execution_service import ExecutionService, ExecutionDecision
from apps.agent_sessions.views import (
    SessionCreateView, SessionDetailView, SessionPauseView, SessionResumeView,
    SessionCancelView, SessionIntentView, SessionPlanView, SessionApproveView,
    SessionNextStepView, SessionActionResultView, SessionPendingConfirmationView,
)
from apps.device_bridge.views import DeviceHeartbeatView

factory = APIRequestFactory()
PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"

errors = []

def check(label, condition, detail=""):
    if condition:
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label}  {detail}")
        errors.append(label)

# ── 1. Session creation ─────────────────────────────────────────────────────
print("\n── Session lifecycle ──")
req = factory.post("/api/agent/sessions/", {
    "device_id": "device-abc",
    "input_mode": "voice",
    "supported_packages": ["com.whatsapp"],
}, format="json")
req.user = type("U", (), {"is_authenticated": False})()
resp = SessionCreateView.as_view()(req)
check("POST /sessions/ → 201", resp.status_code == 201)
session_id = resp.data.get("session_id")
check("Response has session_id", session_id is not None)
check("Response status=created", resp.data.get("status") == "created")

# ── 2. GET session detail ────────────────────────────────────────────────────
req = factory.get(f"/api/agent/sessions/{session_id}/")
req.user = type("U", (), {"is_authenticated": False})()
resp = SessionDetailView.as_view()(req, session_id=session_id)
check("GET /sessions/{id}/ → 200", resp.status_code == 200)
check("Detail has input_mode", "input_mode" in resp.data)
check("Detail has supported_packages", "supported_packages" in resp.data)

# ── 3. Intent ────────────────────────────────────────────────────────────────
print("\n── Intent & Planning ──")
req = factory.post(f"/api/agent/sessions/{session_id}/intent/",
    {"transcript": "Send hello to Alex on WhatsApp"}, format="json")
req.user = type("U", (), {"is_authenticated": False})()
resp = SessionIntentView.as_view()(req, session_id=session_id)
check("POST /sessions/{id}/intent/ → 200", resp.status_code == 200)
intent = resp.data.get("intent", {})
check("Intent has goal", "goal" in intent)
check("Intent detects WhatsApp", intent.get("target_app") == "WhatsApp")
check("Intent has risk_level", "risk_level" in intent)

# ── 4. IntentService unit test ───────────────────────────────────────────────
parsed = IntentService.parse("Open Google Maps and navigate to Central Park")
check("IntentService detects Maps", parsed["target_app"] == "Google Maps")
check("IntentService returns app_package", parsed["app_package"] == "com.google.android.apps.maps")

# ── 5. Plan submission ───────────────────────────────────────────────────────
plan_id = str(uuid.uuid4())
plan_payload = {
    "plan": {
        "plan_id": plan_id,
        "session_id": str(session_id),
        "goal": "Send hello to Alex on WhatsApp",
        "app_package": "com.whatsapp",
        "version": 1,
        "steps": [
            {
                "id": "step_1",
                "type": "OPEN_APP",
                "params": {"package": "com.whatsapp"},
                "expected_outcome": {"screen_hint": "chat_list"},
                "timeout_ms": 5000,
                "retry_policy": {"max_attempts": 2},
                "sensitivity": "low",
                "requires_confirmation": False,
            },
            {
                "id": "step_2",
                "type": "TAP_ELEMENT",
                "params": {"selector": {"view_id": "search_button"}},
                "expected_outcome": {"screen_hint": "search_open"},
                "timeout_ms": 3000,
                "retry_policy": {"max_attempts": 2},
                "sensitivity": "low",
                "requires_confirmation": False,
            },
            # REQUEST_CONFIRMATION must immediately precede the step that requires it
            {
                "id": "step_3",
                "type": "REQUEST_CONFIRMATION",
                "params": {"message": "Send 'hello' to Alex?"},
                "expected_outcome": {"screen_hint": "confirmation_shown"},
                "timeout_ms": 30000,
                "retry_policy": {"max_attempts": 1},
                "sensitivity": "medium",
                "requires_confirmation": False,
            },
            {
                "id": "step_4",
                "type": "TAP_ELEMENT",
                "params": {"selector": {"view_id": "send_button"}},
                "expected_outcome": {"screen_hint": "message_sent"},
                "timeout_ms": 3000,
                "retry_policy": {"max_attempts": 2},
                "sensitivity": "medium",
                "requires_confirmation": True,
            },
        ],
    }
}
req = factory.post(f"/api/agent/sessions/{session_id}/plan/", plan_payload, format="json")
req.user = type("U", (), {"is_authenticated": False})()
resp = SessionPlanView.as_view()(req, session_id=session_id)
check("POST /sessions/{id}/plan/ → 201", resp.status_code == 201, str(resp.data))

# ── 6. Approve ───────────────────────────────────────────────────────────────
req = factory.post(f"/api/agent/sessions/{session_id}/approve/",
    {"plan_id": plan_id, "user_confirmation_mode": "hard"}, format="json")
req.user = type("U", (), {"is_authenticated": False})()
resp = SessionApproveView.as_view()(req, session_id=session_id)
check("POST /sessions/{id}/approve/ → 200", resp.status_code == 200)
check("approve returns approved=true", resp.data.get("approved") is True)
check("approve includes effective_plan", "effective_plan" in resp.data)

# ── 7. Next step ─────────────────────────────────────────────────────────────
print("\n── Execution loop ──")
req = factory.post(f"/api/agent/sessions/{session_id}/next-step/",
    {"plan_id": plan_id, "completed_action_ids": []}, format="json")
req.user = type("U", (), {"is_authenticated": False})()
resp = SessionNextStepView.as_view()(req, session_id=session_id)
check("POST /sessions/{id}/next-step/ → 200", resp.status_code == 200, str(resp.data))
check("next-step returns next_action", "next_action" in resp.data)
check("next-step returns executor_hint", "executor_hint" in resp.data)
check("executor_hint for WhatsApp", resp.data.get("executor_hint") == "whatsapp_send_message_v1")

# ── 8. Action result (success path) ─────────────────────────────────────────
from datetime import datetime, timezone
req = factory.post(f"/api/agent/sessions/{session_id}/action-result/", {
    "plan_id": plan_id,
    "action_id": "step_1",
    "result": {"success": True, "code": "", "message": ""},
    "executed_at": datetime.now(timezone.utc).isoformat(),
    "duration_ms": 450,
}, format="json")
req.user = type("U", (), {"is_authenticated": False})()
resp = SessionActionResultView.as_view()(req, session_id=session_id)
check("POST /sessions/{id}/action-result/ → 200", resp.status_code == 200, str(resp.data))
check("decision status is 'continue'", resp.data.get("status") == "continue")

# ── 9. Pending confirmation (none yet) ──────────────────────────────────────
print("\n── Confirmation ──")
req = factory.get(f"/api/agent/sessions/{session_id}/pending-confirmation/")
req.user = type("U", (), {"is_authenticated": False})()
resp = SessionPendingConfirmationView.as_view()(req, session_id=session_id)
check("GET /pending-confirmation/ → 200", resp.status_code == 200)
check("has_pending=False", resp.data.get("has_pending") is False)

# ── 10. Pause / Resume / Cancel ─────────────────────────────────────────────
print("\n── Pause / Resume / Cancel ──")
# First advance to EXECUTING status
session_obj = AgentSession.objects.get(pk=session_id)
SessionService.transition(session_obj, SessionStatus.EXECUTING)

req = factory.post(f"/api/agent/sessions/{session_id}/pause/")
req.user = type("U", (), {"is_authenticated": False})()
resp = SessionPauseView.as_view()(req, session_id=session_id)
check("POST /pause/ → 200", resp.status_code == 200)
check("Status is paused", resp.data.get("status") == SessionStatus.PAUSED)

req = factory.post(f"/api/agent/sessions/{session_id}/resume/")
req.user = type("U", (), {"is_authenticated": False})()
resp = SessionResumeView.as_view()(req, session_id=session_id)
check("POST /resume/ → 200", resp.status_code == 200)
check("Status restored to executing", resp.data.get("status") == SessionStatus.EXECUTING)

req = factory.post(f"/api/agent/sessions/{session_id}/cancel/")
req.user = type("U", (), {"is_authenticated": False})()
resp = SessionCancelView.as_view()(req, session_id=session_id)
check("POST /cancel/ → 200", resp.status_code == 200)
check("Status is aborted", resp.data.get("status") == SessionStatus.ABORTED)

# ── 11. Heartbeat ────────────────────────────────────────────────────────────
print("\n── Heartbeat ──")
req = factory.post("/api/agent/device/heartbeat/", {
    "session_id": str(session_id),
    "current_step": 1,
    "foreground_package": "com.whatsapp",
}, format="json")
req.user = type("U", (), {"is_authenticated": False})()
resp = DeviceHeartbeatView.as_view()(req)
check("POST /device/heartbeat/ → 200", resp.status_code == 200, str(resp.data))
# Session is aborted so alive=False is expected
check("Heartbeat returns alive key", "alive" in resp.data)
check("Heartbeat returns status key", "status" in resp.data)

# ── 12. ExecutionService unit: decide abort on fatal error ───────────────────
print("\n── ExecutionService decisions ──")
session_obj2 = AgentSession.objects.get(pk=session_id)
plan_obj = ActionPlanRecord.objects.get(session=session_obj2)
# Reset to executing for decision test
AgentSession.objects.filter(pk=session_id).update(status=SessionStatus.EXECUTING, current_step_index=0)
session_obj2.refresh_from_db()
decision = ExecutionService.decide_after_result(
    session=session_obj2,
    plan=plan_obj,
    action_id="step_1",
    result_success=False,
    result_code="SENSITIVE_SCREEN",
)
check("Fatal SENSITIVE_SCREEN → abort", decision.status == "abort")

decision2 = ExecutionService.decide_after_result(
    session=session_obj2,
    plan=plan_obj,
    action_id="step_1",
    result_success=False,
    result_code="ELEMENT_NOT_FOUND",
)
check("Retryable ELEMENT_NOT_FOUND → retry", decision2.status == "retry")
check("Retry includes action_id", decision2.next_action_id == "step_1")

# ── Summary ───────────────────────────────────────────────────────────────────
print()
if errors:
    print(f"\033[91mFailed: {len(errors)} check(s)\033[0m")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("\033[92mAll Stage 5 checks passed.\033[0m")
