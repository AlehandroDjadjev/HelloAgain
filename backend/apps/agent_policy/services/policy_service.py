"""
PolicyEnforcer — the single enforcement point between plan compilation and execution.

Architecture rules:
  - System rules run first and can never be overridden by user policy.
  - User policy can only add restrictions, never remove system-level ones.
  - If any rule blocks the plan, enforcement stops immediately and returns.
  - Confirmation insertion only happens if no prior REQUEST_CONFIRMATION already guards the step.
  - The engine is deterministic: same plan + same policy always produces the same result.
  - Every decision (including "allow") is logged to PolicyDecisionRecord.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import uuid4

from django.db import transaction

from apps.agent_core.enums import ActionSensitivity, ActionType
from apps.agent_core.schemas import ActionPlan, ActionStep, ExpectedOutcome, RetryPolicy

logger = logging.getLogger(__name__)

# ── System-level constants (non-overridable by user policy) ───────────────────

SYSTEM_ALLOWED_PACKAGES: frozenset[str] = frozenset({
    "com.whatsapp",
    "com.google.android.apps.maps",
    "com.android.chrome",
    "com.google.android.gm",
})

SYSTEM_BLOCKED_GOALS: frozenset[str] = frozenset({
    "financial_transfer",
    "change_password",
    "disable_security_feature",
    "delete_account",
    "authorize_payment",
})

SYSTEM_BLOCKED_KEYWORDS: tuple[str, ...] = (
    "bank", "payment", "transfer", "password",
    "2fa", "otp", "security", "pin", "credit card",
)

SYSTEM_MAX_PLAN_LENGTH: int = 20

# Words in a selector that indicate an irreversible UI action
_TAP_TRIGGER_WORDS: tuple[str, ...] = (
    "send", "submit", "delete", "book", "pay",
    "confirm", "order", "purchase", "buy",
)

# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class PolicyDecision:
    rule_name: str
    decision: str          # "allow" | "confirm" | "block"
    reason: str
    action_id: str = ""    # empty for plan-level decisions


@dataclass
class PolicyResult:
    approved: bool
    modified_plan: Optional[ActionPlan]  # None if blocked; the original or modified plan if approved
    blocked_reason: Optional[str]
    policy_decisions: list[PolicyDecision] = field(default_factory=list)
    is_modified: bool = False            # True when confirmation steps were inserted


# ── PolicyEnforcer ────────────────────────────────────────────────────────────

class PolicyEnforcer:
    """
    Main entry point.  Call enforce_policy() and inspect PolicyResult.

    Example:
        result = PolicyEnforcer.enforce_policy(plan, goal_type="send_message",
                                               user_policy=policy_record, session=session)
        if not result.approved:
            raise PermissionDenied(result.blocked_reason)
    """

    @staticmethod
    @transaction.atomic
    def enforce_policy(
        plan: ActionPlan,
        goal_type: str = "",
        user_policy=None,                   # UserAutomationPolicy | None
        session=None,                        # AgentSession | None (for DB logging)
    ) -> PolicyResult:
        decisions: list[PolicyDecision] = []

        # ── Load system policy overrides from DB if an admin has set them ────
        sys_cfg = _load_system_config()

        allowed_packages = sys_cfg.get("allowed_packages") or list(SYSTEM_ALLOWED_PACKAGES)
        blocked_goals    = set(sys_cfg.get("blocked_goals")    or SYSTEM_BLOCKED_GOALS)
        blocked_kw       = list(sys_cfg.get("blocked_keywords") or SYSTEM_BLOCKED_KEYWORDS)
        max_length       = sys_cfg.get("max_plan_length") or SYSTEM_MAX_PLAN_LENGTH

        # Merge user keywords into the keyword list (user can only add, never remove)
        if user_policy and user_policy.blocked_keywords:
            extra = [k for k in user_policy.blocked_keywords if k not in blocked_kw]
            blocked_kw = blocked_kw + extra

        # ── SYSTEM RULE 1: package must be on the allowlist ──────────────────
        if plan.app_package not in allowed_packages:
            d = PolicyDecision(
                rule_name="sys.allowed_packages",
                decision="block",
                reason=f"Package '{plan.app_package}' is not in system ALLOWED_PACKAGES.",
            )
            decisions.append(d)
            result = PolicyResult(approved=False, modified_plan=None,
                                  blocked_reason=d.reason, policy_decisions=decisions)
            _persist_decisions(decisions, plan, session)
            return result

        decisions.append(PolicyDecision(
            rule_name="sys.allowed_packages",
            decision="allow",
            reason=f"Package '{plan.app_package}' is allowed.",
        ))

        # ── SYSTEM RULE 2: blocked goal types ────────────────────────────────
        if goal_type and goal_type in blocked_goals:
            d = PolicyDecision(
                rule_name="sys.blocked_goals",
                decision="block",
                reason=f"Goal type '{goal_type}' is unconditionally blocked.",
            )
            decisions.append(d)
            result = PolicyResult(approved=False, modified_plan=None,
                                  blocked_reason=d.reason, policy_decisions=decisions)
            _persist_decisions(decisions, plan, session)
            return result

        decisions.append(PolicyDecision(
            rule_name="sys.blocked_goals",
            decision="allow",
            reason="No blocked goal type detected.",
        ))

        # ── SYSTEM RULE 3: action types must all be valid enum values ─────────
        valid_types = {t.value for t in ActionType}
        invalid = [s.id for s in plan.steps if s.type.value not in valid_types]
        if invalid:
            d = PolicyDecision(
                rule_name="sys.valid_action_types",
                decision="block",
                reason=f"Steps with invalid action types: {invalid}",
            )
            decisions.append(d)
            result = PolicyResult(approved=False, modified_plan=None,
                                  blocked_reason=d.reason, policy_decisions=decisions)
            _persist_decisions(decisions, plan, session)
            return result

        decisions.append(PolicyDecision(
            rule_name="sys.valid_action_types",
            decision="allow",
            reason="All action types are valid.",
        ))

        # ── SYSTEM RULE 4: high-sensitivity step forces plan risk ≥ medium ───
        has_high = any(s.sensitivity == ActionSensitivity.HIGH for s in plan.steps)
        if has_high:
            decisions.append(PolicyDecision(
                rule_name="sys.risk_escalation",
                decision="allow",
                reason="Plan contains high-sensitivity step — risk level noted.",
            ))

        # ── SYSTEM RULE 5: plan length cap ────────────────────────────────────
        if len(plan.steps) > max_length:
            d = PolicyDecision(
                rule_name="sys.max_plan_length",
                decision="block",
                reason=f"Plan has {len(plan.steps)} steps; system maximum is {max_length}.",
            )
            decisions.append(d)
            result = PolicyResult(approved=False, modified_plan=None,
                                  blocked_reason=d.reason, policy_decisions=decisions)
            _persist_decisions(decisions, plan, session)
            return result

        decisions.append(PolicyDecision(
            rule_name="sys.max_plan_length",
            decision="allow",
            reason=f"Plan length {len(plan.steps)} is within the limit of {max_length}.",
        ))

        # ── SYSTEM RULE 6: blocked keywords scan ─────────────────────────────
        for step in plan.steps:
            hit = _find_blocked_keyword(step.params, blocked_kw)
            if hit:
                d = PolicyDecision(
                    rule_name="sys.blocked_keywords",
                    decision="block",
                    reason=f"Blocked keyword '{hit}' detected in step '{step.id}' params.",
                    action_id=step.id,
                )
                decisions.append(d)
                result = PolicyResult(approved=False, modified_plan=None,
                                      blocked_reason=d.reason, policy_decisions=decisions)
                _persist_decisions(decisions, plan, session)
                return result

        decisions.append(PolicyDecision(
            rule_name="sys.blocked_keywords",
            decision="allow",
            reason="No blocked keywords found in any action params.",
        ))

        # ── USER RULE: allow_text_entry ───────────────────────────────────────
        if user_policy and not user_policy.allow_text_entry:
            type_steps = [s.id for s in plan.steps if s.type == ActionType.TYPE_TEXT]
            if type_steps:
                d = PolicyDecision(
                    rule_name="user.allow_text_entry",
                    decision="block",
                    reason=f"User policy disallows TYPE_TEXT actions. Affected: {type_steps}",
                )
                decisions.append(d)
                result = PolicyResult(approved=False, modified_plan=None,
                                      blocked_reason=d.reason, policy_decisions=decisions)
                _persist_decisions(decisions, plan, session)
                return result

        decisions.append(PolicyDecision(
            rule_name="user.allow_text_entry",
            decision="allow",
            reason="Text entry is permitted.",
        ))

        # ── USER RULE: allow_send_actions ─────────────────────────────────────
        if user_policy and not user_policy.allow_send_actions:
            goal_lower = (plan.goal or "").lower()
            if "send" in goal_lower or goal_type in ("send_message", "draft_email"):
                d = PolicyDecision(
                    rule_name="user.allow_send_actions",
                    decision="block",
                    reason="User policy disallows send/compose actions.",
                )
                decisions.append(d)
                result = PolicyResult(approved=False, modified_plan=None,
                                      blocked_reason=d.reason, policy_decisions=decisions)
                _persist_decisions(decisions, plan, session)
                return result

        decisions.append(PolicyDecision(
            rule_name="user.allow_send_actions",
            decision="allow",
            reason="Send actions are permitted.",
        ))

        # ── USER RULE: user-scoped blocked action types ───────────────────────
        if user_policy and user_policy.blocked_action_types:
            blocked_at = set(user_policy.blocked_action_types)
            blocked_steps = [s.id for s in plan.steps if s.type.value in blocked_at]
            if blocked_steps:
                d = PolicyDecision(
                    rule_name="user.blocked_action_types",
                    decision="block",
                    reason=f"User policy blocks action types in steps: {blocked_steps}",
                )
                decisions.append(d)
                result = PolicyResult(approved=False, modified_plan=None,
                                      blocked_reason=d.reason, policy_decisions=decisions)
                _persist_decisions(decisions, plan, session)
                return result

        decisions.append(PolicyDecision(
            rule_name="user.blocked_action_types",
            decision="allow",
            reason="No user-blocked action types found.",
        ))

        # ── USER RULE: user-scoped allowed packages (can only narrow system list)
        if user_policy and user_policy.allowed_packages:
            # Intersect — if user list doesn't include the plan's package, block
            effective_pkgs = set(allowed_packages) & set(user_policy.allowed_packages)
            if plan.app_package not in effective_pkgs:
                d = PolicyDecision(
                    rule_name="user.allowed_packages",
                    decision="block",
                    reason=(
                        f"Package '{plan.app_package}' is not in the user's "
                        f"allowed_packages list."
                    ),
                )
                decisions.append(d)
                result = PolicyResult(approved=False, modified_plan=None,
                                      blocked_reason=d.reason, policy_decisions=decisions)
                _persist_decisions(decisions, plan, session)
                return result

        decisions.append(PolicyDecision(
            rule_name="user.allowed_packages",
            decision="allow",
            reason="Package passes user package restriction.",
        ))

        # ── CONFIRMATION INSERTION ────────────────────────────────────────────
        hard_confirm_for_send = bool(
            user_policy and user_policy.require_hard_confirmation_for_send
        )
        modified_steps, conf_decisions = _insert_confirmations(
            plan.steps, hard_confirm_for_send
        )
        decisions.extend(conf_decisions)
        is_modified = len(modified_steps) != len(plan.steps)

        # Rebuild the plan if steps were modified
        if is_modified:
            try:
                effective_plan = ActionPlan.model_validate({
                    "plan_id":     plan.plan_id,
                    "session_id":  plan.session_id,
                    "goal":        plan.goal,
                    "app_package": plan.app_package,
                    "steps":       [s.model_dump(mode="json") for s in modified_steps],
                    "version":     plan.version,
                })
            except Exception as exc:
                logger.error("Policy confirmation insertion produced an invalid plan: %s", exc)
                d = PolicyDecision(
                    rule_name="sys.post_insertion_validation",
                    decision="block",
                    reason=f"Plan failed re-validation after confirmation insertion: {exc}",
                )
                decisions.append(d)
                result = PolicyResult(approved=False, modified_plan=None,
                                      blocked_reason=d.reason, policy_decisions=decisions)
                _persist_decisions(decisions, plan, session)
                return result
        else:
            effective_plan = plan

        _persist_decisions(decisions, effective_plan, session)
        logger.info(
            "Policy enforcement complete for plan %s: approved=True, modified=%s, "
            "decisions=%d",
            plan.plan_id, is_modified, len(decisions),
        )
        return PolicyResult(
            approved=True,
            modified_plan=effective_plan,
            blocked_reason=None,
            policy_decisions=decisions,
            is_modified=is_modified,
        )


# ── Confirmation insertion ────────────────────────────────────────────────────

def _insert_confirmations(
    steps: list[ActionStep],
    hard_confirm_for_send: bool,
) -> tuple[list[ActionStep], list[PolicyDecision]]:
    """
    Walk the step list and insert REQUEST_CONFIRMATION steps where needed.

    Rules:
      1. Before any TAP_ELEMENT whose selector contains a trigger word.
      2. Before any TYPE_TEXT with sensitivity >= medium.

    Never inserts if the immediately preceding step is already REQUEST_CONFIRMATION.
    Sets requires_confirmation=True on any step that gets a confirmation inserted before it.
    """
    new_steps: list[ActionStep] = []
    decisions: list[PolicyDecision] = []

    for step in steps:
        needs_conf = False
        conf_reason = ""

        if step.type == ActionType.TAP_ELEMENT:
            trigger = _tap_trigger_word(step.params)
            if trigger:
                needs_conf = True
                conf_reason = f"TAP_ELEMENT selector contains trigger word '{trigger}'"
        elif step.type == ActionType.TYPE_TEXT:
            if step.sensitivity in (ActionSensitivity.MEDIUM, ActionSensitivity.HIGH):
                needs_conf = True
                conf_reason = f"TYPE_TEXT with sensitivity='{step.sensitivity.value}'"

        if needs_conf:
            prev_is_conf = (
                bool(new_steps)
                and new_steps[-1].type == ActionType.REQUEST_CONFIRMATION
            )
            if not prev_is_conf:
                conf_step = _make_confirmation_step(step, hard_confirm_for_send)
                new_steps.append(conf_step)
                # Mark original step as requiring the preceding confirmation
                step = step.model_copy(update={"requires_confirmation": True})
                decisions.append(PolicyDecision(
                    rule_name="sys.confirmation_insertion",
                    decision="confirm",
                    reason=conf_reason,
                    action_id=step.id,
                ))
            else:
                # Already guarded — upgrade to hard confirmation if required
                if hard_confirm_for_send and _tap_trigger_word(step.params) in (
                    "send", "submit", "pay", "purchase", "buy", "order"
                ):
                    prev = new_steps[-1]
                    upgraded_params = dict(prev.params)
                    upgraded_params["hard_confirmation"] = True
                    new_steps[-1] = prev.model_copy(update={
                        "params": upgraded_params,
                        "sensitivity": ActionSensitivity.HIGH,
                    })
                    decisions.append(PolicyDecision(
                        rule_name="user.require_hard_confirmation_for_send",
                        decision="confirm",
                        reason=f"Upgraded confirmation to hard for step '{step.id}'",
                        action_id=step.id,
                    ))
                else:
                    decisions.append(PolicyDecision(
                        rule_name="sys.confirmation_insertion",
                        decision="allow",
                        reason=f"Step '{step.id}' already preceded by REQUEST_CONFIRMATION.",
                        action_id=step.id,
                    ))
        new_steps.append(step)

    return new_steps, decisions


def _make_confirmation_step(trigger_step: ActionStep, hard: bool) -> ActionStep:
    """Build a REQUEST_CONFIRMATION step to insert before trigger_step."""
    sel = trigger_step.params.get("selector", {}) or {}
    label_parts = [
        v for k in ("content_desc", "text", "view_id")
        if (v := sel.get(k))
    ]
    action_label = trigger_step.type.value.replace("_", " ").title()
    selector_label = f" [{', '.join(label_parts)}]" if label_parts else ""

    params: dict = {
        "action_summary": f"About to {action_label}{selector_label}",
    }
    if hard:
        params["hard_confirmation"] = True

    return ActionStep(
        id=f"pol_{trigger_step.id}",
        type=ActionType.REQUEST_CONFIRMATION,
        params=params,
        expected_outcome=ExpectedOutcome(screen_hint="confirmation_shown"),
        timeout_ms=0,
        retry_policy=RetryPolicy(max_attempts=1),
        sensitivity=ActionSensitivity.HIGH if hard else ActionSensitivity.MEDIUM,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tap_trigger_word(params: dict) -> str:
    """Return the first trigger word found in TAP_ELEMENT selector strings, or ''."""
    sel = params.get("selector", {}) or {}
    combined = " ".join(
        str(v).lower()
        for v in sel.values()
        if isinstance(v, str)
    )
    for word in _TAP_TRIGGER_WORDS:
        if word in combined:
            return word
    return ""


def _find_blocked_keyword(params: dict, keywords: list[str]) -> str:
    """Recursively scan all string values in params for blocked keywords. Returns first hit."""
    combined = _extract_strings(params).lower()
    for kw in keywords:
        if kw in combined:
            return kw
    return ""


def _extract_strings(obj: Any) -> str:
    """Flatten all nested string values in a dict/list into one space-separated string."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return " ".join(_extract_strings(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return " ".join(_extract_strings(v) for v in obj)
    return ""


def _load_system_config() -> dict:
    """Load admin-managed system policy overrides from DB. Returns {} if none saved."""
    try:
        from apps.agent_policy.models import SystemPolicyConfig
        cfg = SystemPolicyConfig.objects.first()
        if cfg:
            return {
                "allowed_packages": cfg.allowed_packages or [],
                "blocked_goals":    cfg.blocked_goals or [],
                "blocked_keywords": cfg.blocked_keywords or [],
                "max_plan_length":  cfg.max_plan_length,
            }
    except Exception:
        pass
    return {}


def _persist_decisions(
    decisions: list[PolicyDecision],
    plan: ActionPlan,
    session,
) -> None:
    """Bulk-create PolicyDecisionRecord rows. Silently swallows errors."""
    if session is None:
        return
    try:
        from apps.agent_policy.models import PolicyDecisionRecord
        PolicyDecisionRecord.objects.bulk_create([
            PolicyDecisionRecord(
                session=session,
                plan_id=plan.plan_id,
                rule_name=d.rule_name,
                action_id=d.action_id,
                decision=d.decision,
                reason=d.reason,
            )
            for d in decisions
        ])
    except Exception as exc:
        logger.warning("Failed to persist policy decisions: %s", exc)
