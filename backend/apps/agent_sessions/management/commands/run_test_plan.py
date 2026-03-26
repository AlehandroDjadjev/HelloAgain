"""
Management command: run_test_plan

Simulates the execution loop for a compiled plan without a real Android device.
Each step is presented to the operator, who provides a mock result via stdin
(or automatic success is used with --auto flag).

Usage:
    python manage.py run_test_plan --plan-file whatsapp_test.json [--auto] [--session-id abc123]

Plan file format (same as ActionPlan JSON schema):
{
    "goal":        "Send WhatsApp to Alex: running late",
    "app_package": "com.whatsapp",
    "steps":       [ { "id": "wa_1", "type": "OPEN_APP", "params": {...}, ... }, ... ]
}

Interactive mode:
    After displaying each step the command prompts:
        [s]uccess / [f]ail <code> / [q]uit
    Press Enter for success (default).

Auto mode (--auto):
    Every step succeeds automatically. Useful for CI / smoke-testing the loop logic.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Optional

from django.core.management.base import BaseCommand, CommandError

from apps.agent_plans.models import ActionPlanRecord, PlanStatus
from apps.agent_plans.services import PlanService
from apps.agent_sessions.execution_service import (
    ExecutionService,
    SESSION_TIMEOUT_SECONDS,
    MAX_STEPS_PER_SESSION,
)
from apps.agent_sessions.models import AgentSession, SessionStatus
from apps.agent_sessions.services import SessionService

# ── ANSI colours ──────────────────────────────────────────────────────────────

R = "\033[91m"   # red
G = "\033[92m"   # green
Y = "\033[93m"   # yellow
B = "\033[94m"   # blue
C = "\033[96m"   # cyan
X = "\033[0m"    # reset


class Command(BaseCommand):
    help = "Simulate the plan execution loop without a real Android device."

    def add_arguments(self, parser):
        parser.add_argument(
            "--plan-file", required=True, metavar="FILE",
            help="Path to ActionPlan JSON file.",
        )
        parser.add_argument(
            "--auto", action="store_true",
            help="Auto-succeed every step (no user prompts).",
        )
        parser.add_argument(
            "--session-id", default="", metavar="ID",
            help="Use a specific session ID (default: auto-generated UUID).",
        )
        parser.add_argument(
            "--user-id", default="test_operator", metavar="UID",
            help="User ID to associate with the test session.",
        )

    def handle(self, *args, **options):
        plan_path = Path(options["plan_file"])
        if not plan_path.exists():
            raise CommandError(f"Plan file not found: {plan_path}")

        try:
            raw = json.loads(plan_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON: {exc}") from exc

        session_id_str = options["session_id"] or uuid.uuid4().hex
        auto = options["auto"]
        user_id = options["user_id"]

        self.stdout.write(f"\n{B}run_test_plan — HelloAgain Execution Harness{X}")
        self.stdout.write(f"Plan:      {plan_path.name}")
        self.stdout.write(f"Session:   {session_id_str}")
        self.stdout.write(f"Auto-mode: {'yes' if auto else 'no (interactive)'}\n")

        # ── Create DB objects ─────────────────────────────────────────────────
        session = SessionService.create(
            user_id=user_id,
            device_id="test_harness",
            supported_packages=[raw.get("app_package", "com.whatsapp")],
        )
        self.stdout.write(f"Created session {session.id}")

        # Store intent stub
        PlanService.store_intent(
            session=session,
            raw_transcript=raw.get("goal", "test"),
            parsed_intent={"goal": raw.get("goal", "test")},
        )

        # Validate and store the plan
        from apps.agent_core.schemas import ActionPlan as ActionPlanSchema
        try:
            validated_plan = ActionPlanSchema.model_validate({
                "plan_id":     uuid.uuid4().hex,
                "session_id":  str(session.id),
                "goal":        raw.get("goal", "test"),
                "app_package": raw.get("app_package", "com.whatsapp"),
                "steps":       raw.get("steps", []),
            })
        except Exception as exc:
            raise CommandError(f"Plan validation failed: {exc}") from exc

        plan_record = PlanService.store_plan(session, validated_plan)
        plan_record.status = PlanStatus.APPROVED
        plan_record.save(update_fields=["status"])
        SessionService.transition(session, SessionStatus.APPROVED)

        self.stdout.write(
            f"Plan stored: {plan_record.id} "
            f"({plan_record.step_count} steps, app={plan_record.app_package})\n"
        )

        # ── Execution loop ────────────────────────────────────────────────────
        step_num = 0
        completed: list[str] = []
        last_result: Optional[dict] = None

        while True:
            session.refresh_from_db()
            plan_record.refresh_from_db()

            resp = ExecutionService.get_next_action(
                session=session,
                plan=plan_record,
                screen_state=None,  # No real device
                completed_action_ids=completed,
                last_action_result=last_result,
            )

            status_line = f"{C}[{resp.status.upper()}]{X}"

            if resp.status == "complete":
                self.stdout.write(f"\n{G}✔  COMPLETE — all steps executed successfully.{X}\n")
                break

            if resp.status == "abort":
                self.stdout.write(f"\n{R}✘  ABORT: {resp.reason}{X}\n")
                break

            if resp.status == "manual_takeover":
                self.stdout.write(f"\n{Y}⚠  MANUAL TAKEOVER: {resp.reason}{X}\n")
                break

            if resp.status == "confirm":
                action = resp.next_action or {}
                params = action.get("params", {}) or {}
                action_id = action.get("id", "")
                self.stdout.write(
                    f"\n{Y}[CONFIRMATION REQUIRED]{X}  step={action_id}\n"
                    f"  Summary: {params.get('action_summary','')}\n"
                    f"  Recipient: {params.get('recipient','')}\n"
                    f"  Preview: {params.get('content_preview','')[:80]}\n"
                )
                if not auto:
                    ans = self._prompt("  Approve? [Y/n]: ").strip().lower()
                    if ans in ("n", "no"):
                        last_result = {"success": False, "code": "CONFIRMATION_REJECTED"}
                        # Mark any pending ConfirmationRecord as rejected
                        from apps.agent_sessions.models import ConfirmationRecord as CR
                        CR.objects.filter(
                            session=session, step_id=action_id,
                            status=CR.Status.PENDING,
                        ).update(status=CR.Status.REJECTED)
                        decision = ExecutionService.decide_after_result(
                            session=session, plan=plan_record,
                            action_id=action_id,
                            result_success=False, result_code="CONFIRMATION_REJECTED",
                        )
                        self.stdout.write(f"  {R}Rejected.{X}")
                        if decision.status == "abort":
                            break
                        continue

                # Auto-approve: resolve the ConfirmationRecord, then advance the step
                from apps.agent_sessions.models import ConfirmationRecord as CR
                CR.objects.filter(
                    session=session, step_id=action_id,
                    status=CR.Status.PENDING,
                ).update(status=CR.Status.APPROVED)
                self._record_result(session, plan_record, action_id, True, "OK", completed)
                last_result = {"success": True, "code": "OK"}
                step_num += 1
                continue

            # status == "execute"
            step = resp.next_action or {}
            step_num += 1
            step_id   = step.get("id", "?")
            step_type = step.get("type", "?")
            params    = step.get("params", {}) or {}
            timeout   = step.get("timeout_ms", 5000)

            self.stdout.write(
                f"\n{status_line} Step {step_num:02d}  {B}{step_id}{X}  "
                f"{step_type}  timeout={timeout}ms"
            )
            self.stdout.write(f"  params: {json.dumps(params, ensure_ascii=False)[:120]}")
            if resp.executor_hint:
                self.stdout.write(f"  hint:   {resp.executor_hint}")

            if auto:
                result_success, result_code = True, "OK"
                self.stdout.write(f"  {G}→ auto-success{X}")
            else:
                ans = self._prompt("  Result [Enter=success / f <code> / q]: ").strip()
                if ans.lower() == "q":
                    self.stdout.write(f"{Y}Quit by operator.{X}")
                    break
                if ans.lower().startswith("f"):
                    parts = ans.split(maxsplit=1)
                    result_success = False
                    result_code = parts[1].upper() if len(parts) > 1 else "UNKNOWN"
                    self.stdout.write(f"  {R}→ fail ({result_code}){X}")
                else:
                    result_success, result_code = True, "OK"
                    self.stdout.write(f"  {G}→ success{X}")

            self._record_result(
                session, plan_record, step_id, result_success, result_code, completed
            )
            last_result = {"success": result_success, "code": result_code}

            decision = ExecutionService.decide_after_result(
                session=session,
                plan=plan_record,
                action_id=step_id,
                result_success=result_success,
                result_code=result_code,
            )
            session.refresh_from_db()
            self.stdout.write(f"  decision={decision.status}")

            if decision.status == "complete":
                self.stdout.write(f"\n{G}✔  COMPLETE — all steps executed successfully.{X}\n")
                break
            if decision.status == "abort":
                self.stdout.write(f"\n{R}✘  Execution aborted.{X}")
                break

        self.stdout.write(
            f"\nFinal session status: {session.status}  "
            f"(steps executed: {session.current_step_index})\n"
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _prompt(self, msg: str) -> str:
        self.stdout.write(msg, ending="")
        self.stdout.flush()
        return input()

    @staticmethod
    def _record_result(
        session: AgentSession,
        plan: ActionPlanRecord,
        step_id: str,
        success: bool,
        code: str,
        completed: list[str],
    ) -> None:
        from apps.device_bridge.services import DeviceBridgeService
        from apps.agent_core.enums import ActionResultStatus
        from datetime import datetime, timezone as tz

        step_type = ""
        for s in (plan.steps or []):
            if s.get("id") == step_id:
                step_type = s.get("type", "")
                break

        DeviceBridgeService.record_action_result(
            session=session,
            plan_id=plan.id,
            step_id=step_id,
            step_type=step_type,
            status=ActionResultStatus.SUCCESS.value if success else ActionResultStatus.FAILURE.value,
            error_code="" if success else code,
            error_detail="",
            executed_at=datetime.now(tz.utc),
            duration_ms=0,
        )
        if success:
            completed.append(step_id)
        session.refresh_from_db()
