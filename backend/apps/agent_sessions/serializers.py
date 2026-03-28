from rest_framework import serializers

from .models import AgentSession, ConfirmationRecord, ReasoningProvider


# ---------------------------------------------------------------------------
# Session read serializers
# ---------------------------------------------------------------------------

class AgentSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentSession
        fields = [
            "id", "user_id", "device_id", "input_mode", "reasoning_provider",
            "supported_packages", "status", "current_step_index",
            "created_at", "updated_at",
        ]
        read_only_fields = fields


class AgentSessionDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentSession
        fields = [
            "id", "user_id", "device_id", "input_mode", "reasoning_provider",
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
    reasoning_provider = serializers.ChoiceField(
        choices=ReasoningProvider.values,
        default=ReasoningProvider.OPENAI,
    )
    supported_packages = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
        default=list,
    )


class SessionCreateResponseSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    status = serializers.CharField()
    reasoning_provider = serializers.ChoiceField(choices=ReasoningProvider.values)


class AgentCommandSubmitSerializer(serializers.Serializer):
    """
    POST /api/agent/command/
    One-shot convenience wrapper for the session-based automation pipeline.
    """
    prompt = serializers.CharField(min_length=1)
    device_id = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        default="",
    )
    input_mode = serializers.ChoiceField(choices=["voice", "text"], default="text")
    reasoning_provider = serializers.ChoiceField(
        choices=ReasoningProvider.values,
        default=ReasoningProvider.OPENAI,
    )
    supported_packages = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
        default=list,
    )


class NavigationPrepareSerializer(serializers.Serializer):
    prompt = serializers.CharField(min_length=1)
    device_id = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        default="",
    )
    supported_packages = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
        default=list,
    )


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
    """
    POST /api/agent/sessions/{id}/next-step/

    LLM mode  (no plan): only screen_state is required.
    Plan mode (backward compat): plan_id may be supplied; completed_action_ids
      and last_action_result are accepted but ignored (history is in step_history).
    """
    plan_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    screen_state = serializers.JSONField(required=False, allow_null=True, default=None)
    # Kept for backward compatibility — ignored in LLM flow
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

    LLM mode: plan_id is optional. action_type and reasoning are new optional
      fields that carry back what the LLM decided so decide_after_result()
      can record them in step_history without another lookup.
    Plan mode (backward compat): plan_id still accepted.
    """
    plan_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    action_id = serializers.CharField()
    result = ActionResultInputSerializer()
    screen_state = serializers.JSONField(required=False, allow_null=True, default=None)
    duration_ms = serializers.IntegerField(min_value=0, default=0)
    executed_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    # LLM-mode extras (ignored in plan mode)
    action_type = serializers.CharField(required=False, allow_blank=True, default="")
    reasoning   = serializers.CharField(required=False, allow_blank=True, default="")
    screen_hash_before = serializers.CharField(required=False, allow_blank=True, default="")


class ExecutionDecisionSerializer(serializers.Serializer):
    status = serializers.CharField()
    next_action_id = serializers.CharField(required=False, allow_null=True, default=None)
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    reasoning = serializers.CharField(required=False, allow_blank=True, default="")


class ReasonedStepResponseSerializer(serializers.Serializer):
    """
    Embeds LLM decision metadata in the next-step response so the Flutter UI
    can display the agent's reasoning to the user in real time.
    """
    action_type           = serializers.CharField()
    params                = serializers.DictField()
    reasoning             = serializers.CharField()
    confidence            = serializers.FloatField()
    requires_confirmation = serializers.BooleanField()
    sensitivity           = serializers.CharField()


class IntentReadyResponseSerializer(serializers.Serializer):
    """
    Response from POST /sessions/{id}/intent/ in LLM-mode.
    execution_ready=True means the frontend can start calling /next-step/ immediately.
    """
    intent            = serializers.DictField()
    execution_ready   = serializers.BooleanField()
    can_auto_compile  = serializers.BooleanField()
    session_status    = serializers.CharField()


class AgentCommandResponseSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    session_status = serializers.CharField()
    reasoning_provider = serializers.ChoiceField(choices=ReasoningProvider.values)
    intent = serializers.DictField()
    execution_ready = serializers.BooleanField()
    can_auto_compile = serializers.BooleanField()


class NavigationPrepareResponseSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    session_status = serializers.CharField()
    intent = serializers.DictField()
    execution_ready = serializers.BooleanField()
    debug = serializers.DictField(required=False)


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
