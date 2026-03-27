"""
Management command: run_llm_session

Simulates an LLM-driven session against a directory of mock screen states.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.agent_core.llm_client import _PROVIDER_DEFAULTS
from apps.agent_sessions.confirmation_service import ConfirmationService
from apps.agent_sessions.execution_service import ExecutionService
from apps.agent_sessions.models import SessionStatus
from apps.agent_sessions.services import SessionService

R = "\033[91m"
G = "\033[92m"
Y = "\033[93m"
B = "\033[94m"
C = "\033[96m"
X = "\033[0m"


class Command(BaseCommand):
    help = "Run an LLM-driven mock session against recorded screen-state JSON files."

    def add_arguments(self, parser):
        parser.add_argument("--goal", required=True, help="Goal text for the session.")
        parser.add_argument("--target-app", required=True, help="Android package name to target.")
        parser.add_argument(
            "--mock-screens",
            required=True,
            help="Directory containing step_*.json mock screen files.",
        )
        parser.add_argument(
            "--provider",
            default="groq",
            choices=sorted(_PROVIDER_DEFAULTS.keys()),
            help="LLM provider to use for this run (default: groq).",
        )
        parser.add_argument(
            "--auto-confirm",
            action="store_true",
            help="Auto-approve confirmations without prompting.",
        )
        parser.add_argument(
            "--user-id",
            default="llm_harness",
            help="User ID to attach to the test session.",
        )

    def handle(self, *args, **options):
        goal = options["goal"].strip()
        target_app = options["target_app"].strip()
        mock_dir = Path(options["mock_screens"])
        provider = options["provider"].strip().lower()
        auto_confirm = bool(options["auto_confirm"])

        if not mock_dir.exists() or not mock_dir.is_dir():
            raise CommandError(f"Mock screen directory not found: {mock_dir}")

        screen_files = sorted(mock_dir.glob("step_*.json"))
        if not screen_files:
            raise CommandError(
                f"No screen files found in {mock_dir}. Expected files like step_0_home.json."
            )

        old_provider = getattr(settings, "LLM_PROVIDER", "")
        settings.LLM_PROVIDER = provider
        os.environ["LLM_PROVIDER"] = provider

        session = SessionService.create(
            user_id=options["user_id"],
            device_id="mock_device",
            transcript=goal,
            input_mode="text",
            supported_packages=[target_app],
        )
        session.store_intent_data(goal=goal, target_app=target_app, entities={})
        SessionService.transition(session, SessionStatus.EXECUTING)

        self.stdout.write(f"\n{B}run_llm_session{X}")
        self.stdout.write(f"Goal:       {goal}")
        self.stdout.write(f"Target app: {target_app}")
        self.stdout.write(f"Provider:   {provider}")
        self.stdout.write(f"Screens:    {len(screen_files)} from {mock_dir}\n")

        exit_code = 1
        timeline: list[str] = []
        screen_index = 0

        try:
            while screen_index < len(screen_files):
                payload = self._load_mock_file(screen_files[screen_index])
                screen_state = payload.get("screen_state") if "screen_state" in payload else payload
                simulated_result = (
                    payload.get("result")
                    or payload.get("simulated_result")
                    or {"success": True, "code": "OK"}
                )

                response = ExecutionService.get_next_action(
                    session=session,
                    plan=None,
                    screen_state=screen_state,
                )

                self.stdout.write(
                    f"{C}[screen {screen_index}]{X} {screen_files[screen_index].name} -> "
                    f"{response.status.upper()}"
                )
                if response.reasoning:
                    self.stdout.write(f"  reasoning: {response.reasoning}")
                if response.next_action:
                    self.stdout.write(
                        "  action: "
                        + json.dumps(
                            {
                                "type": response.next_action.get("type"),
                                "params": response.next_action.get("params", {}),
                            },
                            ensure_ascii=False,
                        )
                    )

                timeline.append(
                    f"{screen_files[screen_index].name}: {response.status} "
                    f"{response.next_action.get('type') if response.next_action else ''}".strip()
                )

                if response.status == "complete":
                    exit_code = 0
                    break

                if response.status in {"abort", "manual_takeover"}:
                    self.stdout.write(f"  {R}{response.reason}{X}")
                    break

                if response.status == "confirm":
                    pending = ConfirmationService.get_pending(session)
                    if pending is None:
                        raise CommandError("Execution requested confirmation but no pending record exists.")

                    approved = auto_confirm or self._prompt_approval(pending.action_summary)
                    ConfirmationService.resolve(pending.id, approved=approved, session=session)
                    timeline.append(
                        f"confirmation:{'approved' if approved else 'rejected'}:{pending.step_id}"
                    )
                    if not approved:
                        self.stdout.write(f"  {R}Confirmation rejected.{X}")
                        break
                    self.stdout.write(f"  {G}Confirmation approved.{X}")
                    continue

                action = response.next_action or {}
                decision = ExecutionService.decide_after_result(
                    session=session,
                    plan=None,
                    action_id=str(action.get("id") or ""),
                    result_success=bool(simulated_result.get("success", True)),
                    result_code=str(simulated_result.get("code") or "OK"),
                    action_type=str(action.get("type") or ""),
                    params=action.get("params") or {},
                    reasoning=response.reasoning,
                    screen_hash_before=str(screen_state.get("screen_hash") or ""),
                    screen_hash_after=str(
                        simulated_result.get("screen_hash_after") or screen_state.get("screen_hash") or ""
                    ),
                )
                timeline.append(
                    f"result:{action.get('type')}:{simulated_result.get('code', 'OK')}:{decision.status}"
                )
                self.stdout.write(
                    f"  simulated result: {simulated_result.get('code', 'OK')} -> {decision.status}"
                )

                if decision.status in {"abort", "manual_takeover"}:
                    break

                screen_index += 1

            session.refresh_from_db()
            self.stdout.write("\nSession timeline:")
            for line in timeline:
                self.stdout.write(f"  - {line}")

            if session.step_history:
                self.stdout.write("\nRecorded steps:")
                for entry in session.step_history:
                    self.stdout.write(
                        f"  - Step {entry.get('step_index')}: {entry.get('action_type')} "
                        f"{'OK' if entry.get('result_success') else entry.get('result_code')} "
                        f"| {entry.get('reasoning', '')}"
                    )

            confirmations = list(session.confirmations.order_by("created_at"))
            if confirmations:
                self.stdout.write("\nConfirmations:")
                for conf in confirmations:
                    self.stdout.write(
                        f"  - {conf.step_id}: {conf.status} | {conf.action_summary}"
                    )

            self.stdout.write(f"\nFinal session status: {session.status}")
            if exit_code == 0:
                self.stdout.write(f"{G}Session completed successfully.{X}")
            else:
                self.stdout.write(f"{R}Session did not complete successfully.{X}")
        finally:
            settings.LLM_PROVIDER = old_provider
            if old_provider:
                os.environ["LLM_PROVIDER"] = old_provider
            elif "LLM_PROVIDER" in os.environ:
                del os.environ["LLM_PROVIDER"]

        if exit_code != 0:
            raise SystemExit(1)

    @staticmethod
    def _load_mock_file(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON in {path}: {exc}") from exc

    def _prompt_approval(self, summary: str) -> bool:
        answer = input(f"Approve confirmation '{summary}'? [Y/n]: ").strip().lower()
        return answer not in {"n", "no"}
