import uuid
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Session({self.id}, user={self.user_id}, status={self.status})"


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
