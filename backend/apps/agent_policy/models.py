import uuid
from django.db import models

from apps.agent_core.enums import ActionType, RiskLevel, SensitiveScreenPolicy

_ACTION_TYPE_CHOICES = [(t.value, t.value) for t in ActionType]


class UserAutomationPolicy(models.Model):
    """
    Per-user (or per-org) safety rules evaluated before plan approval.

    User policy can only RESTRICT the system defaults — it cannot expand them.
    For example a user cannot add packages to ALLOWED_PACKAGES, only remove them.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    org_id  = models.CharField(max_length=255, blank=True, default="", db_index=True)

    # ── Package access ────────────────────────────────────────────────────────
    allowed_packages = models.JSONField(
        default=list,
        help_text=(
            "User-specific allowlist. Empty = use system defaults. "
            "Intersected with system ALLOWED_PACKAGES — cannot expand it."
        ),
    )

    # ── Action-level controls ─────────────────────────────────────────────────
    blocked_action_types = models.JSONField(
        default=list,
        help_text="ActionType values the policy unconditionally rejects.",
    )
    always_confirm_action_types = models.JSONField(
        default=list,
        help_text="ActionType values that always require a confirmation step.",
    )
    allow_text_entry = models.BooleanField(
        default=True,
        help_text="If False, any TYPE_TEXT step will be blocked.",
    )
    allow_send_actions = models.BooleanField(
        default=True,
        help_text=(
            "If False, plans whose goal contains 'send' (messages, email) "
            "are blocked entirely."
        ),
    )
    require_hard_confirmation_for_send = models.BooleanField(
        default=False,
        help_text=(
            "If True, REQUEST_CONFIRMATION steps preceding send/submit actions "
            "are marked hard_confirmation=true so the UI shows a stricter dialog."
        ),
    )

    # ── Keyword controls ──────────────────────────────────────────────────────
    blocked_keywords = models.JSONField(
        default=list,
        help_text=(
            "Additional user-specific blocked keywords (merged with system list). "
            "If found in any action param, the plan is blocked."
        ),
    )

    # ── Risk ──────────────────────────────────────────────────────────────────
    max_steps_per_plan = models.PositiveIntegerField(default=30)
    sensitive_screen_policy = models.CharField(
        max_length=16,
        choices=[(p.value, p.value) for p in SensitiveScreenPolicy],
        default=SensitiveScreenPolicy.ABORT.value,
    )
    risk_threshold = models.CharField(
        max_length=16,
        choices=[(r.value, r.value) for r in RiskLevel],
        default=RiskLevel.MEDIUM.value,
    )
    allow_coordinates_fallback = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user_id", "org_id"],
                name="unique_policy_per_user_org",
            )
        ]
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"Policy(user={self.user_id or '*'}, org={self.org_id or '*'})"


class SystemPolicyConfig(models.Model):
    """
    Admin-controlled override of the system-wide hardcoded policy constants.
    There should only ever be ONE row — managed via the update_system_policy
    management command or Django admin.

    If no row exists the hardcoded defaults in policy_service.py apply.
    """

    # Only one row is expected — enforced by always using pk=1
    allowed_packages = models.JSONField(
        default=list,
        help_text="System-level package allowlist. Overrides the hardcoded ALLOWED_PACKAGES.",
    )
    blocked_goals = models.JSONField(
        default=list,
        help_text="goal_type values that are unconditionally blocked.",
    )
    blocked_keywords = models.JSONField(
        default=list,
        help_text="Substrings that if found in any action param immediately block the plan.",
    )
    max_plan_length = models.PositiveIntegerField(
        default=20,
        help_text="Hard cap on the number of steps. Plans exceeding this are blocked.",
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        verbose_name = "System Policy Config"

    def __str__(self) -> str:
        return f"SystemPolicyConfig (updated {self.updated_at})"


class PolicyDecisionOutcome(models.TextChoices):
    ALLOW   = "allow",   "Allow"
    CONFIRM = "confirm", "Confirm (inserted REQUEST_CONFIRMATION)"
    BLOCK   = "block",   "Block"


class PolicyDecisionRecord(models.Model):
    """
    Immutable log of every policy decision made during plan enforcement.
    One record per rule evaluation — even "allow" decisions are logged.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        "agent_sessions.AgentSession",
        on_delete=models.CASCADE,
        related_name="policy_decisions",
    )
    plan_id = models.CharField(
        max_length=64, blank=True, default="",
        help_text="UUID of the ActionPlanRecord being evaluated.",
    )
    rule_name = models.CharField(max_length=128, db_index=True)
    action_id = models.CharField(
        max_length=128, blank=True, default="",
        help_text="Step id if this decision applies to a specific action; empty for plan-level rules.",
    )
    action_type = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="ActionType value for step-level policy checks.",
    )
    decision = models.CharField(
        max_length=16,
        choices=PolicyDecisionOutcome.choices,
        db_index=True,
    )
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["session", "plan_id"]),
        ]

    def __str__(self) -> str:
        return f"PolicyDecision({self.rule_name}, {self.decision}, session={self.session_id})"
