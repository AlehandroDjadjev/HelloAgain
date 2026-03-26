"""
Management command: update_system_policy

Reads a JSON configuration file and updates (or creates) the singleton
SystemPolicyConfig record in the database.

Usage:
    python manage.py update_system_policy --config path/to/policy.json [--dry-run]

JSON format (all fields optional — omitted fields keep their current values):
{
    "allowed_packages":  ["com.whatsapp", "com.android.chrome"],
    "blocked_goals":     ["financial_transfer", "change_password"],
    "blocked_keywords":  ["bank", "payment", "otp"],
    "max_plan_length":   20
}

Reset to built-in defaults:
    python manage.py update_system_policy --reset
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.agent_policy.models import SystemPolicyConfig
from apps.agent_policy.services import (
    SYSTEM_ALLOWED_PACKAGES,
    SYSTEM_BLOCKED_GOALS,
    SYSTEM_BLOCKED_KEYWORDS,
    SYSTEM_MAX_PLAN_LENGTH,
)


class Command(BaseCommand):
    help = "Update system-level policy configuration from a JSON file."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--config",
            metavar="FILE",
            help="Path to JSON config file.",
        )
        group.add_argument(
            "--reset",
            action="store_true",
            help="Reset system policy to built-in hardcoded defaults.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would change without writing to the database.",
        )
        parser.add_argument(
            "--updated-by",
            default="",
            metavar="NAME",
            help="Username or identifier to record as the author of this change.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        if options["reset"]:
            new_values = {
                "allowed_packages": sorted(SYSTEM_ALLOWED_PACKAGES),
                "blocked_goals":    sorted(SYSTEM_BLOCKED_GOALS),
                "blocked_keywords": list(SYSTEM_BLOCKED_KEYWORDS),
                "max_plan_length":  SYSTEM_MAX_PLAN_LENGTH,
            }
        else:
            config_path = Path(options["config"])
            if not config_path.exists():
                raise CommandError(f"Config file not found: {config_path}")

            try:
                raw = config_path.read_text(encoding="utf-8")
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CommandError(f"Invalid JSON in {config_path}: {exc}") from exc

            new_values = self._validate_and_merge(data)

        self.stdout.write("Proposed system policy:")
        self.stdout.write(json.dumps(new_values, indent=2))

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — no changes written."))
            return

        cfg, created = SystemPolicyConfig.objects.get_or_create(pk=1)
        cfg.allowed_packages = new_values["allowed_packages"]
        cfg.blocked_goals    = new_values["blocked_goals"]
        cfg.blocked_keywords = new_values["blocked_keywords"]
        cfg.max_plan_length  = new_values["max_plan_length"]
        cfg.updated_by       = options.get("updated_by", "")
        cfg.save()

        action = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{action} SystemPolicyConfig (pk=1)."))

    def _validate_and_merge(self, data: dict) -> dict:
        """Merge provided values onto the current DB record (or hardcoded defaults)."""
        try:
            current = SystemPolicyConfig.objects.get(pk=1)
            base = {
                "allowed_packages": current.allowed_packages,
                "blocked_goals":    current.blocked_goals,
                "blocked_keywords": current.blocked_keywords,
                "max_plan_length":  current.max_plan_length,
            }
        except SystemPolicyConfig.DoesNotExist:
            base = {
                "allowed_packages": sorted(SYSTEM_ALLOWED_PACKAGES),
                "blocked_goals":    sorted(SYSTEM_BLOCKED_GOALS),
                "blocked_keywords": list(SYSTEM_BLOCKED_KEYWORDS),
                "max_plan_length":  SYSTEM_MAX_PLAN_LENGTH,
            }

        if "allowed_packages" in data:
            if not isinstance(data["allowed_packages"], list):
                raise CommandError("'allowed_packages' must be a list of strings.")
            base["allowed_packages"] = data["allowed_packages"]

        if "blocked_goals" in data:
            if not isinstance(data["blocked_goals"], list):
                raise CommandError("'blocked_goals' must be a list of strings.")
            base["blocked_goals"] = data["blocked_goals"]

        if "blocked_keywords" in data:
            if not isinstance(data["blocked_keywords"], list):
                raise CommandError("'blocked_keywords' must be a list of strings.")
            base["blocked_keywords"] = data["blocked_keywords"]

        if "max_plan_length" in data:
            val = data["max_plan_length"]
            if not isinstance(val, int) or val < 1 or val > 100:
                raise CommandError("'max_plan_length' must be an integer between 1 and 100.")
            base["max_plan_length"] = val

        unknown = set(data.keys()) - {"allowed_packages", "blocked_goals", "blocked_keywords", "max_plan_length"}
        if unknown:
            self.stdout.write(self.style.WARNING(f"Ignoring unknown keys: {unknown}"))

        return base
