from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers


class RegisterSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    display_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    phone_number = serializers.CharField(max_length=32, required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    onboarding_answers = serializers.DictField(
        child=serializers.CharField(allow_blank=True),
        required=False,
    )
    contacts_permission_granted = serializers.BooleanField(required=False, default=False)
    share_phone_with_friends = serializers.BooleanField(required=False, default=True)
    share_email_with_friends = serializers.BooleanField(required=False, default=True)

    def validate_username(self, value: str) -> str:
        value = value.strip()
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("This username is already taken.")
        return value

    def validate_email(self, value: str) -> str:
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("This email is already registered.")
        return value

    def validate_password(self, value: str) -> str:
        validate_password(value)
        return value


class LoginSerializer(serializers.Serializer):
    identifier = serializers.CharField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class AccountProfileUpdateSerializer(serializers.Serializer):
    display_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    phone_number = serializers.CharField(max_length=32, required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    onboarding_answers = serializers.DictField(
        child=serializers.CharField(allow_blank=True),
        required=False,
    )
    contacts_permission_granted = serializers.BooleanField(required=False)
    share_phone_with_friends = serializers.BooleanField(required=False)
    share_email_with_friends = serializers.BooleanField(required=False)
    home_lat = serializers.FloatField(required=False, min_value=-90.0, max_value=90.0)
    home_lng = serializers.FloatField(required=False, min_value=-180.0, max_value=180.0)


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
