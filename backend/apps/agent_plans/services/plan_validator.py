"""
PlanValidator — pre-execution validation layer on top of Pydantic schema checks.

Pydantic validates structure and types.
PlanValidator adds policy-level rules:
  - target app must be in the allowed packages list
  - no blocked action types
  - required params present for each action type
  - REQUEST_CONFIRMATION ordering (also enforced by Pydantic, but surfaced with clearer messages here)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from apps.agent_core.enums import ActionType
from apps.agent_core.schemas import ActionPlan

logger = logging.getLogger(__name__)

# ── Required params per action type ──────────────────────────────────────────

_REQUIRED_PARAMS: dict[str, list[str]] = {
    ActionType.OPEN_APP.value:                ["package"],
    ActionType.WAIT_FOR_APP.value:            ["package"],
    ActionType.WAIT_FOR_ELEMENT.value:        ["selector"],
    ActionType.FIND_ELEMENT.value:            ["selector"],
    ActionType.TAP_ELEMENT.value:             ["selector"],
    ActionType.LONG_PRESS_ELEMENT.value:      ["selector"],
    ActionType.FOCUS_ELEMENT.value:           ["selector"],
    ActionType.TYPE_TEXT.value:               ["text"],
    ActionType.SCROLL.value:                  ["direction"],
    ActionType.SWIPE.value:                   ["start_x", "start_y", "end_x", "end_y"],
    ActionType.ASSERT_SCREEN.value:           ["screen_hint"],
    ActionType.ASSERT_ELEMENT.value:          ["selector"],
    ActionType.REQUEST_CONFIRMATION.value:    ["action_summary"],
    ActionType.ABORT.value:                   ["reason"],
    # GET_SCREEN_STATE, CLEAR_TEXT, BACK, HOME — no required params
}


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.is_valid = False

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)


# ── PlanValidator ─────────────────────────────────────────────────────────────

class PlanValidator:
    """
    Validate a compiled ActionPlan against policy and structural rules.

    Usage:
        result = PlanValidator.validate(plan, allowed_packages=session.supported_packages)
        if not result.is_valid:
            raise ValueError(result.errors)
    """

    @staticmethod
    def validate(
        plan: ActionPlan,
        allowed_packages: list[str] | None = None,
        blocked_action_types: list[str] | None = None,
        max_steps: int = 30,
    ) -> ValidationResult:
        result = ValidationResult(is_valid=True)

        # 1. Plan must have at least one step
        if not plan.steps:
            result.add_error("Plan has no steps.")
            return result   # nothing else to check

        # 2. Step count limit
        if len(plan.steps) > max_steps:
            result.add_error(
                f"Plan has {len(plan.steps)} steps which exceeds the limit of {max_steps}."
            )

        # 3. Target app must be in allowed packages
        if allowed_packages:
            if plan.app_package not in allowed_packages:
                result.add_error(
                    f"App package '{plan.app_package}' is not in the allowed packages list: "
                    f"{allowed_packages}."
                )

        # 4. Per-step checks
        valid_action_types = {t.value for t in ActionType}
        for i, step in enumerate(plan.steps):
            prefix = f"Step {i+1} (id={step.id!r}, type={step.type.value!r})"

            # 4a. Action type must be a valid enum value
            if step.type.value not in valid_action_types:
                result.add_error(
                    f"{prefix}: Unknown action type '{step.type.value}'."
                )
                continue

            # 4b. No blocked action types
            if blocked_action_types and step.type.value in blocked_action_types:
                result.add_error(
                    f"{prefix}: Action type '{step.type.value}' is blocked by policy."
                )

            # 4c. Required params present
            required = _REQUIRED_PARAMS.get(step.type.value, [])
            for param_key in required:
                val = step.params.get(param_key)
                if val is None or val == "" or val == {}:
                    result.add_error(
                        f"{prefix}: Missing required param '{param_key}'."
                    )

            # 4d. REQUEST_CONFIRMATION ordering
            #     (Pydantic already enforces this, but we surface it here too)
            if step.requires_confirmation:
                if i == 0 or plan.steps[i - 1].type != ActionType.REQUEST_CONFIRMATION:
                    result.add_error(
                        f"{prefix}: requires_confirmation=True but the preceding step "
                        f"is not REQUEST_CONFIRMATION."
                    )

        # 5. Warn if plan has send/irreversible actions but no REQUEST_CONFIRMATION
        has_irreversible = any(
            s.type in (ActionType.ABORT,) or s.requires_confirmation
            for s in plan.steps
        )
        has_confirmation = any(
            s.type == ActionType.REQUEST_CONFIRMATION for s in plan.steps
        )
        if has_irreversible and not has_confirmation:
            result.add_warning(
                "Plan contains irreversible steps but no REQUEST_CONFIRMATION step."
            )

        if result.errors:
            logger.warning(
                "Plan %s failed validation with %d error(s): %s",
                plan.plan_id, len(result.errors), result.errors,
            )
        return result
