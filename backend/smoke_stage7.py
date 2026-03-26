"""
Stage 7 smoke test — policy enforcement engine.
Tests: system rules, confirmation insertion, user policy, management command dry-run.
Run: python smoke_stage7.py
"""
import os, sys, logging
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
logging.getLogger("apps.agent_core.llm_client").setLevel(logging.ERROR)
logging.getLogger("apps.agent_plans.services.intent_service").setLevel(logging.ERROR)
logging.disable(logging.WARNING)

import django
# Force keyword fallback for smoke tests — prevents model download
os.environ["LLM_PROVIDER"] = "ollama"
os.environ["LLM_TIMEOUT"]  = "1"
django.setup()
logging.disable(logging.NOTSET)

from apps.agent_core.schemas import ActionPlan, ActionStep, ExpectedOutcome
from apps.agent_core.enums import ActionType, ActionSensitivity
from apps.agent_plans.services import IntentService, PlanCompiler
from apps.agent_policy.services import PolicyEnforcer, SYSTEM_ALLOWED_PACKAGES
from apps.agent_policy.models import UserAutomationPolicy

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
failures = []

def check(label, condition, detail=""):
    if condition:
        print(f"{PASS}  {label}")
    else:
        print(f"{FAIL}  {label}  {detail}")
        failures.append(label)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_wa_plan(session_id="s-test") -> ActionPlan:
    svc = IntentService()
    intent = svc.parse_intent("Send Alex on WhatsApp I am running late", ["com.whatsapp"])
    return PlanCompiler.compile(intent, session_id)


def make_blocked_plan() -> ActionPlan:
    return ActionPlan.model_validate({
        "plan_id": "blocked-test",
        "session_id": "s-test",
        "goal": "Open unknown app",
        "app_package": "com.evil.malware",
        "steps": [
            {"id": "s1", "type": "OPEN_APP", "params": {"package": "com.evil.malware"},
             "expected_outcome": {"screen_hint": "x"}}
        ],
    })


def make_keyword_plan() -> ActionPlan:
    return ActionPlan.model_validate({
        "plan_id": "keyword-test",
        "session_id": "s-test",
        "goal": "Pay for something",
        "app_package": "com.whatsapp",
        "steps": [
            {"id": "s1", "type": "TYPE_TEXT",
             "params": {"text": "my bank password is 1234"},
             "expected_outcome": {"screen_hint": "x"}}
        ],
    })


# ── Test 1: valid WhatsApp plan passes ────────────────────────────────────────

plan = make_wa_plan()
result = PolicyEnforcer.enforce_policy(plan, goal_type="send_message")
check("RULE sys.allowed_packages: com.whatsapp is allowed", result.approved)
check("Confirmation steps inserted for WhatsApp send", result.is_modified)
# After insertion there should be at least one extra REQUEST_CONFIRMATION
conf_count = sum(1 for s in result.modified_plan.steps
                 if s.type == ActionType.REQUEST_CONFIRMATION)
check("At least one REQUEST_CONFIRMATION step in modified plan", conf_count >= 1,
      f"got {conf_count}")

# ── Test 2: disallowed package is blocked ─────────────────────────────────────

bad = make_blocked_plan()
result2 = PolicyEnforcer.enforce_policy(bad)
check("RULE sys.allowed_packages: com.evil.malware is blocked", not result2.approved)
check("blocked_reason is set", bool(result2.blocked_reason))

# ── Test 3: blocked goal type ─────────────────────────────────────────────────

result3 = PolicyEnforcer.enforce_policy(plan, goal_type="financial_transfer")
check("RULE sys.blocked_goals: financial_transfer is blocked", not result3.approved)

# ── Test 4: blocked keyword in params ────────────────────────────────────────

kw_plan = make_keyword_plan()
result4 = PolicyEnforcer.enforce_policy(kw_plan)
check("RULE sys.blocked_keywords: 'password' in TYPE_TEXT params blocked", not result4.approved)

# ── Test 5: plan too long ─────────────────────────────────────────────────────

long_steps = []
for i in range(21):
    long_steps.append({
        "id": f"s{i}", "type": "GET_SCREEN_STATE", "params": {},
        "expected_outcome": {"screen_hint": "x"},
    })
long_plan = ActionPlan.model_validate({
    "plan_id": "long-test", "session_id": "s-test",
    "goal": "Very long plan", "app_package": "com.whatsapp",
    "steps": long_steps,
})
result5 = PolicyEnforcer.enforce_policy(long_plan)
check("RULE sys.max_plan_length: 21-step plan blocked", not result5.approved)

# ── Test 6: user policy — allow_text_entry=False blocks TYPE_TEXT ─────────────

policy_no_text = UserAutomationPolicy(
    user_id="test-user",
    allow_text_entry=False,
)
result6 = PolicyEnforcer.enforce_policy(plan, goal_type="send_message",
                                        user_policy=policy_no_text)
check("USER allow_text_entry=False blocks WhatsApp plan (has TYPE_TEXT)", not result6.approved)

# ── Test 7: user policy — allow_send_actions=False blocks send plan ───────────

policy_no_send = UserAutomationPolicy(
    user_id="test-user",
    allow_send_actions=False,
)
result7 = PolicyEnforcer.enforce_policy(plan, goal_type="send_message",
                                        user_policy=policy_no_send)
check("USER allow_send_actions=False blocks send_message plan", not result7.approved)

# ── Test 8: user policy — package restriction narrows system allowlist ─────────

policy_narrow = UserAutomationPolicy(
    user_id="test-user",
    allowed_packages=["com.android.chrome"],  # only Chrome; WhatsApp excluded
)
result8 = PolicyEnforcer.enforce_policy(plan, user_policy=policy_narrow)
check("USER allowed_packages intersection blocks com.whatsapp", not result8.approved)

# ── Test 9: hard confirmation for send ───────────────────────────────────────

policy_hard = UserAutomationPolicy(
    user_id="test-user",
    require_hard_confirmation_for_send=True,
)
result9 = PolicyEnforcer.enforce_policy(plan, goal_type="send_message",
                                        user_policy=policy_hard)
check("Hard confirmation policy: plan still approved", result9.approved)
hard_conf = any(
    s.params.get("hard_confirmation")
    for s in (result9.modified_plan or plan).steps
    if s.type == ActionType.REQUEST_CONFIRMATION
)
check("Hard confirmation flag set on at least one confirmation step", hard_conf)

# ── Test 10: management command dry-run ──────────────────────────────────────

from django.core.management import call_command
from io import StringIO
import tempfile, json as _json

policy_json = _json.dumps({
    "allowed_packages": ["com.whatsapp"],
    "blocked_goals": ["financial_transfer"],
    "blocked_keywords": ["bank", "otp"],
    "max_plan_length": 15,
})
with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
    f.write(policy_json)
    tmp_path = f.name

out = StringIO()
try:
    call_command("update_system_policy", config=tmp_path, dry_run=True, stdout=out)
    output = out.getvalue()
    check("Management command dry-run runs without error",
          "Dry run" in output or "max_plan_length" in output, output[:200])
except Exception as exc:
    check("Management command dry-run runs without error", False, str(exc))

# ── Summary ───────────────────────────────────────────────────────────────────

print()
if failures:
    print(f"FAILURES ({len(failures)}): {failures}")
    sys.exit(1)
else:
    print("ALL PASS")
