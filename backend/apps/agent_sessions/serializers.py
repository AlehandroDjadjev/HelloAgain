from rest_framework import serializers

from .models import AgentSession, ConfirmationRecord


# ---------------------------------------------------------------------------
# Session read serializers
# ---------------------------------------------------------------------------

class AgentSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentSession
        fields = [
            "id", "user_id", "device_id", "input_mode",
            "supported_packages", "status", "current_step_index",
            "created_at", "updated_at",
        ]
        read_only_fields = fields


class AgentSessionDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentSession
        fields = [
            "id", "user_id", "device_id", "input_mode",
            "supported_packages", "status", "previous_status",
            "current_step_index", "last_heartbeat_at",
            "created_at", "updated_at",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Session lifecycle request serializers
# ---------------------------------------------------------------------------

class AgentSessionCreateSerializer(serializers.Serializer):
    """
    POST /api/agent/sessions/
    user_id is injected from request.user in the view; device supplies the rest.
    """
    device_id = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    input_mode = serializers.ChoiceField(choices=["voice", "text"], default="voice")
    supported_packages = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
        default=list,
    )


class SessionCreateResponseSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    status = serializers.CharField()


# ---------------------------------------------------------------------------
# Intent serializers
# ---------------------------------------------------------------------------

class IntentSubmitSerializer(serializers.Serializer):
    transcript = serializers.CharField(min_length=1)


class IntentResponseSerializer(serializers.Serializer):
    intent = serializers.DictField()


# ---------------------------------------------------------------------------
# Plan approval serializer
# ---------------------------------------------------------------------------

class SessionApproveSerializer(serializers.Serializer):
    plan_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    user_confirmation_mode = serializers.ChoiceField(
        choices=["hard", "soft"], default="hard"
    )


# ---------------------------------------------------------------------------
# Execution loop serializers
# ---------------------------------------------------------------------------

class LastActionResultSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    code = serializers.CharField(required=False, allow_blank=True, default="")


class NextStepRequestSerializer(serializers.Serializer):
    plan_id = serializers.UUIDField()
    screen_state = serializers.JSONField(required=False, allow_null=True, default=None)
    completed_action_ids = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    last_action_result = LastActionResultSerializer(required=False, allow_null=True, default=None)


class ActionResultInputSerializer(serializers.Serializer):
    """Inner result object for ActionResultV2Serializer."""
    success = serializers.BooleanField()
    code = serializers.CharField(required=False, allow_blank=True, default="")
    message = serializers.CharField(required=False, allow_blank=True, default="")


class ActionResultV2Serializer(serializers.Serializer):
    """
    POST /api/agent/sessions/{id}/action-result/
    New shape: action_id (not step_id), nested result object.
    """
    plan_id = serializers.UUIDField()
    action_id = serializers.CharField()          # maps to step_id
    result = ActionResultInputSerializer()
    screen_state = serializers.JSONField(required=False, allow_null=True, default=None)
    duration_ms = serializers.IntegerField(min_value=0, default=0)
    executed_at = serializers.DateTimeField(required=False, allow_null=True, default=None)


class ExecutionDecisionSerializer(serializers.Serializer):
    status = serializers.CharField()
    next_action_id = serializers.CharField(required=False, allow_null=True, default=None)


# ---------------------------------------------------------------------------
# Confirmation serializers
# ---------------------------------------------------------------------------

class ConfirmationRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConfirmationRecord
        fields = [
            "id", "session", "plan_id", "step_id",
            "app_name", "app_package", "action_summary",
            "recipient", "content_preview", "sensitivity",
            "status", "created_at", "expires_at", "resolved_at",
        ]
        read_only_fields = ["id", "created_at", "resolved_at"]


class PendingConfirmationResponseSerializer(serializers.Serializer):
    has_pending = serializers.BooleanField()
    confirmation = ConfirmationRecordSerializer(required=False, allow_null=True)
