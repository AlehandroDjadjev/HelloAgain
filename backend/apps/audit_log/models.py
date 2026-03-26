import uuid
from django.db import models


class AuditEventType(models.TextChoices):
    SESSION_CREATED = "session_created", "Session Created"
    INTENT_STORED = "intent_stored", "Intent Stored"
    PLAN_COMPILED = "plan_compiled", "Plan Compiled"
    PLAN_APPROVED = "plan_approved", "Plan Approved"
    PLAN_REJECTED = "plan_rejected", "Plan Rejected"
    STEP_DISPATCHED = "step_dispatched", "Step Dispatched"
    STEP_SUCCEEDED = "step_succeeded", "Step Succeeded"
    STEP_FAILED = "step_failed", "Step Failed"
    CONFIRMATION_REQUESTED = "confirmation_requested", "Confirmation Requested"
    CONFIRMATION_APPROVED = "confirmation_approved", "Confirmation Approved"
    CONFIRMATION_REJECTED = "confirmation_rejected", "Confirmation Rejected"
    SENSITIVE_SCREEN_DETECTED = "sensitive_screen_detected", "Sensitive Screen Detected"
    SESSION_PAUSED = "session_paused", "Session Paused"
    SESSION_RESUMED = "session_resumed", "Session Resumed"
    SESSION_CANCELLED = "session_cancelled", "Session Cancelled"
    SESSION_ABORTED = "session_aborted", "Session Aborted"
    SESSION_COMPLETED = "session_completed", "Session Completed"
    POLICY_VIOLATION = "policy_violation", "Policy Violation"
    POLICY_ENFORCED  = "policy_enforced",  "Policy Enforced"
    PLAN_BLOCKED     = "plan_blocked",     "Plan Blocked by Policy"


class AuditActor(models.TextChoices):
    USER = "user", "User"
    SYSTEM = "system", "System"
    ANDROID = "android", "Android"


class AuditRecord(models.Model):
    """
    Immutable audit trail for every significant event in a session.
    payload is always structured/redacted — raw screenshots are never stored.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        "agent_sessions.AgentSession",
        on_delete=models.CASCADE,
        related_name="audit_records",
    )
    event_type = models.CharField(
        max_length=64,
        choices=AuditEventType.choices,
        db_index=True,
    )
    actor = models.CharField(
        max_length=16,
        choices=AuditActor.choices,
        default=AuditActor.SYSTEM,
    )
    payload = models.JSONField(
        default=dict,
        help_text="Structured event data. Raw screenshots and sensitive values are never stored.",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"Audit({self.event_type}, session={self.session_id})"
