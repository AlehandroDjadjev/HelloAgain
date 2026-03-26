from rest_framework import serializers

from apps.agent_core.schemas import ScreenState as ScreenStateSchema
from pydantic import ValidationError as PydanticValidationError

from .models import AgentActionEvent, DeviceScreenState


class DeviceScreenStateSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceScreenState
        fields = [
            "id", "session", "step_id",
            "foreground_package", "window_title", "screen_hash",
            "focused_element_ref", "is_sensitive", "nodes", "captured_at",
        ]
        read_only_fields = ["id"]


class ScreenStateIngestSerializer(serializers.Serializer):
    """
    Accepts a raw ScreenState dict from Android, validates against the
    Pydantic schema, then hands off to DeviceBridgeService for persistence.
    """

    session_id = serializers.UUIDField()
    step_id = serializers.CharField(required=False, default="", allow_blank=True)
    screen_state = serializers.JSONField()

    def validate_screen_state(self, value: dict) -> dict:
        try:
            ScreenStateSchema.model_validate(value)
        except PydanticValidationError as exc:
            raise serializers.ValidationError(exc.errors()) from exc
        return value


class AgentActionEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentActionEvent
        fields = [
            "id", "session", "plan_id", "step_id", "step_type",
            "status", "error_code", "error_detail",
            "screen_state", "duration_ms", "executed_at",
        ]
        read_only_fields = ["id"]


class HeartbeatSerializer(serializers.Serializer):
    """POST /api/agent/device/heartbeat/"""
    session_id = serializers.UUIDField()
    current_step = serializers.IntegerField(min_value=0, default=0)
    foreground_package = serializers.CharField(
        required=False, allow_blank=True, default=""
    )


class ActionResultIngestSerializer(serializers.Serializer):
    """
    Accepts an ActionResult payload from Android.
    session_id must match the URL parameter.
    """

    step_id = serializers.CharField()
    plan_id = serializers.UUIDField()
    step_type = serializers.CharField()
    status = serializers.ChoiceField(
        choices=["success", "failure", "timeout", "aborted", "skipped"]
    )
    error_code = serializers.CharField(required=False, allow_blank=True, default="")
    error_detail = serializers.CharField(required=False, allow_blank=True, default="")
    screen_state = serializers.JSONField(required=False, allow_null=True, default=None)
    duration_ms = serializers.IntegerField(min_value=0, default=0)
    executed_at = serializers.DateTimeField()

    def validate_screen_state(self, value) -> dict | None:
        if value is None:
            return None
        try:
            ScreenStateSchema.model_validate(value)
        except PydanticValidationError as exc:
            raise serializers.ValidationError(exc.errors()) from exc
        return value
