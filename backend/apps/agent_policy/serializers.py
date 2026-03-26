from rest_framework import serializers

from apps.agent_core.enums import ActionType

from .models import UserAutomationPolicy

_VALID_ACTION_TYPES = {t.value for t in ActionType}


class UserAutomationPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = UserAutomationPolicy
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_allowed_packages(self, value: list) -> list:
        if not value:
            raise serializers.ValidationError("allowed_packages must not be empty.")
        return value

    def _validate_action_type_list(self, field_name: str, value: list) -> list:
        invalid = [v for v in value if v not in _VALID_ACTION_TYPES]
        if invalid:
            raise serializers.ValidationError(
                f"Unknown action types in {field_name}: {invalid}"
            )
        return value

    def validate_blocked_action_types(self, value: list) -> list:
        return self._validate_action_type_list("blocked_action_types", value)

    def validate_always_confirm_action_types(self, value: list) -> list:
        return self._validate_action_type_list("always_confirm_action_types", value)
