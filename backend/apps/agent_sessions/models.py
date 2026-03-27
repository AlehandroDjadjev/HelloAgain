import uuid
from datetime import datetime, timezone

from django.db import models


class SessionStatus(models.TextChoices):
    CREATED = "created", "Created"
    PLANNING = "planning", "Planning"
    PLAN_READY = "plan_ready", "Plan Ready"
    APPROVED = "approved", "Approved"
    EXECUTING = "executing", "Executing"
    AWAITING_CONFIRMATION = "awaiting_confirmation", "Awaiting Confirmation"
    PAUSED = "paused", "Paused"
    COMPLETED = "completed", "Completed"
    ABORTED = "aborted", "Aborted"
    FAILED = "failed", "Failed"


class InputMode(models.TextChoices):
    VOICE = "voice", "Voice"
    TEXT = "text", "Text"


class ReasoningProvider(models.TextChoices):
    LOCAL = "local", "Local Model"
    OPENAI = "openai", "OpenAI API"


class AgentSession(models.Model):
    """
    Lifecycle container for one user-initiated automation request.
    The transcript is stored here and here only — it never flows into the plan.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.CharField(max_length=255, db_index=True)
    device_id = models.CharField(max_length=255, blank=True, default="")
    input_mode = models.CharField(
        max_length=16, choices=InputMode.choices, default=InputMode.VOICE
    )
    reasoning_provider = models.CharField(
        max_length=16,
        choices=ReasoningProvider.choices,
        default=ReasoningProvider.LOCAL,
        help_text="Which reasoning backend should power intent parsing and step selection.",
    )
    supported_packages = models.JSONField(
        default=list,
        help_text="Android packages the device reports as installed and supported.",
    )
    status = models.CharField(
        max_length=32, choices=SessionStatus.choices, default=SessionStatus.CREATED, db_index=True
    )
    previous_status = models.CharField(
        max_length=32, blank=True, default="",
        help_text="Status snapshot before the session was paused, used for resume.",
    )
    current_step_index = models.PositiveIntegerField(default=0)
    retry_counts = models.JSONField(
        default=dict,
        help_text=(
            "Per-action retry counters: {'action_id': n, '_screen_action_id': n}. "
            "Reset to {} when a new plan is compiled."
        ),
    )
    started_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Timestamp of first EXECUTING transition — used for session-level timeout.",
    )
    transcript = models.TextField(
        blank=True, default="",
        help_text="Raw user input. Never copied into ActionPlan.",
    )
    last_heartbeat_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Last keep-alive ping from the Android device.",
    )
    # ── LLM-in-the-loop intent fields ─────────────────────────────────────────
    # Populated by SessionIntentView when intent is parsed.
    # ExecutionService reads these directly instead of loading an ActionPlanRecord.
    goal = models.TextField(
        blank=True, default="",
        help_text="Short structured goal description from intent parsing.",
    )
    target_app = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Target Android package name (e.g. com.whatsapp).",
    )
    entities = models.JSONField(
        default=dict,
        blank=True,
        help_text="Parsed intent entities: recipient, message, url, query, …",
    )
    risk_level = models.CharField(
        max_length=16, blank=True, default="low",
        help_text="Risk level from intent parsing: low | medium | high.",
    )

    step_history = models.JSONField(
        default=list,
        help_text=(
            "Ordered list of executed steps with results. "
            "Each entry: {step_index, action_type, params, reasoning, "
            "result_code, result_success, screen_hash_before, screen_hash_after, timestamp}. "
            "Never contains raw screenshots or sensitive field content."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Session({self.id}, user={self.user_id}, status={self.status})"

    # ── step_history helpers ───────────────────────────────────────────────

    def append_step(self, step_data: dict) -> None:
        """
        Append one step record to step_history and persist.

        Expected keys in step_data:
          step_index, action_type, params (redacted), reasoning,
          result_code, result_success, screen_hash_before,
          screen_hash_after, timestamp (ISO string).
        """
        if self.step_history is None:
            self.step_history = []
        entry = {
            "step_index":        step_data.get("step_index", len(self.step_history) + 1),
            "action_type":       step_data.get("action_type", ""),
            "params":            _redact_params(step_data.get("params") or {}),
            "reasoning":         step_data.get("reasoning", ""),
            "result_code":       step_data.get("result_code", ""),
            "result_success":    bool(step_data.get("result_success")),
            "screen_hash_before": step_data.get("screen_hash_before", ""),
            "screen_hash_after":  step_data.get("screen_hash_after", ""),
            "timestamp":          step_data.get(
                "timestamp",
                datetime.now(timezone.utc).isoformat(),
            ),
            "is_recovery":       bool(step_data.get("is_recovery", False)),
        }
        self.step_history = list(self.step_history) + [entry]
        self.save(update_fields=["step_history", "updated_at"])

    def get_recent_steps(self, n: int = 10) -> list:
        """Return the last *n* step history entries."""
        history = self.step_history or []
        return list(history[-n:])

    def get_step_count(self) -> int:
        """Total number of steps executed in this session."""
        return len(self.step_history or [])

    def has_llm_intent(self) -> bool:
        """Return True if this session has LLM intent data ready for execution."""
        return bool(self.goal and self.target_app)

    def store_intent_data(
        self,
        goal: str,
        target_app: str,
        entities: dict,
        risk_level: str = "low",
    ) -> None:
        """Persist intent fields and mark the session as ready for LLM execution."""
        self.goal        = goal[:500]
        self.target_app  = target_app
        self.entities    = entities or {}
        self.risk_level  = risk_level or "low"
        self.save(update_fields=["goal", "target_app", "entities", "risk_level", "updated_at"])

    def get_consecutive_failures(self) -> int:
        """
        Count the number of consecutive failed steps at the end of history.
        Resets to 0 on any successful step.
        """
        count = 0
        for entry in reversed(self.step_history or []):
            if entry.get("result_success"):
                break
            count += 1
        return count


# ── Helpers ────────────────────────────────────────────────────────────────────

_SENSITIVE_PARAM_KEYS = frozenset({"text", "content", "message", "body", "password", "otp"})


def _redact_params(params: dict) -> dict:
    """
    Shallow-copy params, replacing sensitive text values with a length hint.
    Selector dicts are kept intact so history is useful for debugging.
    """
    out = {}
    for k, v in params.items():
        if k in _SENSITIVE_PARAM_KEYS and isinstance(v, str) and len(v) > 3:
            out[k] = f"[{len(v)} chars]"
        else:
            out[k] = v
    return out


class ConfirmationRecord(models.Model):
    """
    Persisted ConfirmationRequest. Execution is gated on status = 'approved'.
    Silent auto-approval is never permitted.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        AgentSession, on_delete=models.CASCADE, related_name="confirmations"
    )
    plan_id = models.UUIDField(db_index=True)
    step_id = models.CharField(max_length=64)
    app_name = models.CharField(max_length=128)
    app_package = models.CharField(max_length=255)
    action_summary = models.TextField()
    recipient = models.CharField(max_length=512, blank=True, default="")
    content_preview = models.TextField(blank=True, default="")
    sensitivity = models.CharField(max_length=16)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Confirmation({self.id}, step={self.step_id}, status={self.status})"
