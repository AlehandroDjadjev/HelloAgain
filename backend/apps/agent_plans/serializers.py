from rest_framework import serializers

from apps.agent_core.schemas import ActionPlan as ActionPlanSchema
from pydantic import ValidationError as PydanticValidationError

from .models import ActionPlanRecord, IntentRecord


class IntentRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = IntentRecord
        fields = ["id", "session", "raw_transcript", "parsed_intent", "created_at"]
        read_only_fields = ["id", "created_at"]


class IntentSubmitSerializer(serializers.Serializer):
    transcript = serializers.CharField(min_length=1)
    parsed_intent = serializers.JSONField(required=False, default=dict)


class ActionPlanRecordSerializer(serializers.ModelSerializer):
    step_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = ActionPlanRecord
        fields = [
            "id", "session", "goal", "app_package",
            "steps", "status", "version", "step_count",
            "created_at", "approved_at",
        ]
        read_only_fields = ["id", "status", "step_count", "created_at", "approved_at"]


class ActionPlanSubmitSerializer(serializers.Serializer):
    """
    Accepts a raw plan dict and validates it against the ActionPlan Pydantic schema
    before persisting. This is the single validation gate that prevents
    malformed or natural-language-contaminated plans from entering the DB.
    """

    plan = serializers.JSONField()

    def validate_plan(self, value: dict) -> dict:
        try:
            ActionPlanSchema.model_validate(value)
        except PydanticValidationError as exc:
            raise serializers.ValidationError(exc.errors()) from exc
        return value
