"""
Stage 6 smoke test — intent parsing + plan compilation + validation pipeline.
Run: python smoke_stage6.py
"""
import os, sys, logging
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
logging.getLogger("apps.agent_core.llm_client").setLevel(logging.ERROR)
logging.getLogger("apps.agent_plans.services.intent_service").setLevel(logging.ERROR)

import django
django.setup()

from apps.agent_plans.services import IntentService, PlanCompiler, PlanValidator

TESTS = [
    ("Send Alex on WhatsApp I am running 10 minutes late",            ["com.whatsapp"]),
    ("Navigate to Heathrow Airport",                                   ["com.google.android.apps.maps"]),
    ("Search for best pizza near me",                                  ["com.android.chrome"]),
    ("Open Chrome and go to github.com",                              ["com.android.chrome"]),
    ("Draft an email to boss@example.com saying the report is ready", ["com.google.android.gm"]),
]

svc = IntentService()
failures = []

for transcript, pkgs in TESTS:
    r = svc.parse_intent(transcript, pkgs)
    has_tpl = PlanCompiler.has_template(r.goal_type, r.app_package)
    if not has_tpl:
        print(f"FAIL  No template for {r.goal_type}/{r.app_package}")
        failures.append(transcript)
        continue

    plan = PlanCompiler.compile(r, "smoke-session")
    vr = PlanValidator.validate(plan, allowed_packages=pkgs)

    if vr.is_valid:
        print(f"PASS  [{r.goal_type}/{r.app_package}] {len(plan.steps)} steps  ents={r.entities}")
    else:
        print(f"FAIL  [{r.goal_type}/{r.app_package}] {vr.errors}")
        failures.append(transcript)

print()
if failures:
    print(f"FAILURES ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL PASS")
