from __future__ import annotations

from rest_framework import serializers

from .models import AccountProfile, normalize_phone_number


class RegisterSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    phone_number = serializers.CharField(max_length=32)
    description = serializers.CharField(required=False, allow_blank=True)
    dynamic_profile_summary = serializers.CharField(required=False, allow_blank=True)
    profile_notes = serializers.CharField(required=False, allow_blank=True)
    onboarding_answers = serializers.DictField(
        child=serializers.CharField(allow_blank=True),
        required=False,
    )
    onboarding_completed = serializers.BooleanField(required=False, default=True)
    voice_navigation_enabled = serializers.BooleanField(required=False, default=True)
    microphone_permission_granted = serializers.BooleanField(required=False, default=True)
    phone_permission_granted = serializers.BooleanField(required=False, default=False)
    contacts_permission_granted = serializers.BooleanField(required=False, default=False)
    share_phone_with_friends = serializers.BooleanField(required=False, default=False)
    share_email_with_friends = serializers.BooleanField(required=False, default=False)

    def validate_name(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Please say your name.")
        return value

    def validate_phone_number(self, value: str) -> str:
        normalized = normalize_phone_number(value)
        if not normalized:
            raise serializers.ValidationError("A valid phone number is required.")
        if AccountProfile.objects.filter(normalized_phone_number=normalized).exists():
            raise serializers.ValidationError("This phone number is already registered.")
        return value

    def validate(self, attrs):
        if not attrs.get("phone_permission_granted"):
            raise serializers.ValidationError(
                {"phone_permission_granted": "Phone access is required for sign up."}
            )
        if not attrs.get("microphone_permission_granted"):
            raise serializers.ValidationError(
                {"microphone_permission_granted": "Microphone access is required for voice navigation."}
            )
        return attrs


class LoginSerializer(serializers.Serializer):
    phone_number = serializers.CharField(max_length=32)

    def validate_phone_number(self, value: str) -> str:
        normalized = normalize_phone_number(value)
        if not normalized:
            raise serializers.ValidationError("A valid phone number is required.")
        return value


class AccountProfileUpdateSerializer(serializers.Serializer):
    display_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    phone_number = serializers.CharField(max_length=32, required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    dynamic_profile_summary = serializers.CharField(required=False, allow_blank=True)
    profile_notes = serializers.CharField(required=False, allow_blank=True)
    onboarding_answers = serializers.DictField(
        child=serializers.CharField(allow_blank=True),
        required=False,
    )
    onboarding_completed = serializers.BooleanField(required=False)
    voice_navigation_enabled = serializers.BooleanField(required=False)
    microphone_permission_granted = serializers.BooleanField(required=False)
    phone_permission_granted = serializers.BooleanField(required=False)
    contacts_permission_granted = serializers.BooleanField(required=False)
    share_phone_with_friends = serializers.BooleanField(required=False)
    share_email_with_friends = serializers.BooleanField(required=False)
    home_lat = serializers.FloatField(required=False, min_value=-90.0, max_value=90.0)
    home_lng = serializers.FloatField(required=False, min_value=-180.0, max_value=180.0)

    def validate_phone_number(self, value: str) -> str:
        normalized = normalize_phone_number(value)
        if value and not normalized:
            raise serializers.ValidationError("A valid phone number is required.")
        profile = getattr(self.context.get("profile"), "pk", None)
        if normalized:
            query = AccountProfile.objects.filter(normalized_phone_number=normalized)
            if profile is not None:
                query = query.exclude(pk=profile)
            if query.exists():
                raise serializers.ValidationError("This phone number is already registered.")
        return value


class OnboardingStartSerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=64, required=False, allow_blank=True)


class OnboardingTurnSerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=64)
    message = serializers.CharField()

    def validate_message(self, value: str) -> str:
        value = " ".join(value.split()).strip()
        if not value:
            raise serializers.ValidationError("Please say something so I can continue.")
        return value


class OnboardingConfirmLoginSerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=64)
    phone_confirmed = serializers.BooleanField()
    login_confirmed = serializers.BooleanField()


class OnboardingCompleteSerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=64)
    microphone_permission_granted = serializers.BooleanField(required=False, default=True)
    phone_permission_granted = serializers.BooleanField(required=False, default=True)


class FriendRequestCreateSerializer(serializers.Serializer):
    target_user_id = serializers.IntegerField(required=False, min_value=1)
    target_username = serializers.CharField(required=False, allow_blank=False)
    message = serializers.CharField(required=False, allow_blank=True, max_length=280)

    def validate(self, attrs):
        if not attrs.get("target_user_id") and not attrs.get("target_username"):
            raise serializers.ValidationError(
                "Provide either target_user_id or target_username."
            )
        return attrs


class FriendRequestResponseSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["accept", "decline", "cancel"])


class ContactsImportSerializer(serializers.Serializer):
    replace_existing = serializers.BooleanField(required=False, default=True)
    source = serializers.CharField(required=False, allow_blank=True, max_length=32, default="manual")
    contacts = serializers.ListField(child=serializers.DictField(), allow_empty=True)


class DiscoveryQuerySerializer(serializers.Serializer):
    description = serializers.CharField(allow_blank=False)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=25, default=8)


class RecommendationActivitySerializer(serializers.Serializer):
    event_type = serializers.ChoiceField(
        choices=[
            "profile_viewed",
            "recommendation_clicked",
            "search_result_opened",
            "call_tapped",
            "email_tapped",
        ]
    )
    target_user_id = serializers.IntegerField(required=False, min_value=1)
    discovery_mode = serializers.ChoiceField(
        choices=["for_you", "describe_someone", "search", "direct"],
        required=False,
        default="direct",
    )
    query_text = serializers.CharField(required=False, allow_blank=True)
    metadata = serializers.DictField(required=False, default=dict)


class BoardStateSerializer(serializers.Serializer):
    board_state = serializers.DictField()
    removed_result_id = serializers.CharField(required=False, allow_blank=True)


class ConnectionThreadMessageSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=1000)

    def validate_message(self, value: str) -> str:
        value = " ".join(value.split()).strip()
        if not value:
            raise serializers.ValidationError("Please enter a message.")
        return value


class ConnectionThreadFriendshipActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=["send", "accept", "decline", "cancel", "unfriend"]
    )
    message = serializers.CharField(required=False, allow_blank=True, max_length=280)
