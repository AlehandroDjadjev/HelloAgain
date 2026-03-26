import uuid
from django.db import models


class PlanStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PENDING_APPROVAL = "pending_approval", "Pending Approval"
    APPROVED = "approved", "Approved"
    EXECUTING = "executing", "Executing"
    COMPLETED = "completed", "Completed"
    ABORTED = "aborted", "Aborted"


class IntentRecord(models.Model):
    """
    Stores the raw transcript and the structured intent extracted from it.
    The intent is the bridge between natural language and a typed plan.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.OneToOneField(
        "agent_sessions.AgentSession",
        on_delete=models.CASCADE,
        related_name="intent",
    )
    raw_transcript = models.TextField()
    parsed_intent = models.JSONField(
        default=dict,
        help_text=(
            "Structured intent extracted by LLM. "
            "Example: {app: 'WhatsApp', action: 'send_message', recipient: 'Alice', text: '...'}"
        ),
    )
    # LLM metadata — stored for debugging and audit
    llm_raw_response = models.TextField(
        blank=True,
        default="",
        help_text="Raw JSON string returned by the LLM before parsing.",
    )
    goal_type = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Classified goal type: send_message, open_app, navigate_to, etc.",
    )
    confidence = models.FloatField(
        default=1.0,
        help_text="LLM-reported confidence score 0–1. Below 0.5 flags ambiguity.",
    )
    ambiguity_flags = models.JSONField(
        default=list,
        help_text="List of ambiguity descriptions reported by the LLM.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Intent(session={self.session_id}, goal={self.goal_type})"


class ActionPlanRecord(models.Model):
    """
    Persisted ActionPlan. 'steps' stores the validated JSON list verbatim
    from the ActionPlan Pydantic schema. Immutable once approved.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.OneToOneField(
        "agent_sessions.AgentSession",
        on_delete=models.CASCADE,
        related_name="plan",
    )
    goal = models.CharField(
        max_length=500,
        help_text="Structured goal description. Not a raw transcript.",
    )
    app_package = models.CharField(max_length=255, db_index=True)
    steps = models.JSONField(help_text="Ordered list of ActionStep dicts.")
    status = models.CharField(
        max_length=32,
        choices=PlanStatus.choices,
        default=PlanStatus.DRAFT,
        db_index=True,
    )
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Plan({self.id}, pkg={self.app_package}, status={self.status})"

    @property
    def step_count(self) -> int:
        return len(self.steps) if self.steps else 0
