import uuid
from django.db import models
from django.conf import settings

from apps.agent_core.enums import ActionResultStatus, ActionErrorCode


class DeviceScreenState(models.Model):
    """
    Structured screen snapshot returned by AccessibilityService.
    Never stores raw screenshots. Sensitive-screen fingerprints are stored
    but node trees are redacted when is_sensitive = True.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        "agent_sessions.AgentSession",
        on_delete=models.CASCADE,
        related_name="screen_states",
    )
    step_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="ActionStep.id that triggered this capture, if any.",
    )
    foreground_package = models.CharField(max_length=255, db_index=True)
    window_title = models.CharField(max_length=512, blank=True, default="")
    screen_hash = models.CharField(
        max_length=128,
        db_index=True,
        help_text="Deterministic hash of the accessibility node tree.",
    )
    focused_element_ref = models.CharField(max_length=255, blank=True, default="")
    is_sensitive = models.BooleanField(
        default=False,
        db_index=True,
        help_text="If True, nodes are redacted and execution is halted.",
    )
    nodes = models.JSONField(
        default=list,
        help_text="Flat list of AccessibilityNode dicts. Empty when is_sensitive=True.",
    )
    captured_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-captured_at"]

    def __str__(self) -> str:
        return f"ScreenState(session={self.session_id}, pkg={self.foreground_package}, hash={self.screen_hash[:8]})"

    def save(self, *args, **kwargs):
        if self.is_sensitive and not getattr(settings, "AGENT_UNSAFE_AUTOMATION_MODE", False):
            self.nodes = []
        super().save(*args, **kwargs)


class AgentActionEvent(models.Model):
    """
    One execution record per ActionStep. Posted by Android after each action.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        "agent_sessions.AgentSession",
        on_delete=models.CASCADE,
        related_name="action_events",
    )
    plan_id = models.UUIDField(db_index=True)
    step_id = models.CharField(max_length=64, db_index=True)
    step_type = models.CharField(max_length=32)
    status = models.CharField(
        max_length=16,
        choices=[(s.value, s.value) for s in ActionResultStatus],
        db_index=True,
    )
    error_code = models.CharField(
        max_length=32,
        blank=True,
        default="",
        choices=[("", "")] + [(e.value, e.value) for e in ActionErrorCode],
    )
    error_detail = models.TextField(blank=True, default="")
    screen_state = models.ForeignKey(
        DeviceScreenState,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="action_events",
    )
    duration_ms = models.PositiveIntegerField(default=0)
    executed_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-executed_at"]

    def __str__(self) -> str:
        return f"ActionEvent({self.step_id}, {self.status})"
