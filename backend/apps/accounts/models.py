import re
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from recommendations.models import ElderProfile


def normalize_phone_number(phone_number: str | None) -> str:
    if not phone_number:
        return ""
    trimmed = phone_number.strip()
    if not trimmed:
        return ""
    prefix = "+" if trimmed.startswith("+") else ""
    digits = re.sub(r"\D+", "", trimmed)
    return f"{prefix}{digits}" if digits else ""


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


class ElderlyUser(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    egn = models.CharField(max_length=10, unique=True, help_text="Bulgarian national ID")
    date_of_birth = models.DateField()
    phone = models.CharField(max_length=20, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    home_lat = models.FloatField(blank=True, null=True, help_text="Default home latitude")
    home_lng = models.FloatField(blank=True, null=True, help_text="Default home longitude")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.egn})"


class AccountProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="account_profile",
    )
    elder_profile = models.OneToOneField(
        ElderProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="account_profile",
    )
    display_name = models.CharField(max_length=120)
    phone_number = models.CharField(max_length=32, blank=True)
    normalized_phone_number = models.CharField(max_length=32, blank=True, db_index=True)
    description = models.TextField(blank=True)
    onboarding_answers = models.JSONField(default=dict, blank=True)
    contacts_permission_granted = models.BooleanField(default=False)
    contacts_permission_granted_at = models.DateTimeField(null=True, blank=True)
    share_phone_with_friends = models.BooleanField(default=True)
    share_email_with_friends = models.BooleanField(default=True)
    home_lat = models.FloatField(null=True, blank=True)
    home_lng = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_name", "user_id"]

    def __str__(self):
        return self.display_name or self.user.username

    def save(self, *args, **kwargs):
        self.normalized_phone_number = normalize_phone_number(self.phone_number)
        if not self.display_name:
            self.display_name = self.user.get_full_name().strip() or self.user.username
        if self.contacts_permission_granted and not self.contacts_permission_granted_at:
            self.contacts_permission_granted_at = timezone.now()
        if not self.contacts_permission_granted:
            self.contacts_permission_granted_at = None

        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            fields = set(update_fields)
            fields.update(
                {
                    "normalized_phone_number",
                    "display_name",
                    "contacts_permission_granted_at",
                }
            )
            kwargs["update_fields"] = list(fields)
        super().save(*args, **kwargs)


class AccountToken(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="account_token",
    )
    key = models.CharField(max_length=64, unique=True, default=secrets.token_hex)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_used_at"]

    def __str__(self):
        return f"Token for {self.user.username}"


class FriendRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        CANCELED = "canceled", "Canceled"

    from_profile = models.ForeignKey(
        AccountProfile,
        on_delete=models.CASCADE,
        related_name="sent_friend_requests",
    )
    to_profile = models.ForeignKey(
        AccountProfile,
        on_delete=models.CASCADE,
        related_name="received_friend_requests",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    message = models.CharField(max_length=280, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["from_profile", "to_profile"],
                name="unique_friend_request_direction",
            ),
            models.CheckConstraint(
                condition=~Q(from_profile=F("to_profile")),
                name="friend_request_not_to_self",
            ),
        ]

    def __str__(self):
        return f"{self.from_profile} -> {self.to_profile} ({self.status})"


class ImportedContact(models.Model):
    owner = models.ForeignKey(
        AccountProfile,
        on_delete=models.CASCADE,
        related_name="imported_contacts",
    )
    full_name = models.CharField(max_length=150, blank=True)
    phone_number = models.CharField(max_length=32, blank=True)
    normalized_phone_number = models.CharField(max_length=32, blank=True, db_index=True)
    email = models.EmailField(blank=True)
    normalized_email = models.CharField(max_length=254, blank=True, db_index=True)
    source = models.CharField(max_length=32, default="manual")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["full_name", "id"]

    def __str__(self):
        label = self.full_name or self.phone_number or self.email or "Unnamed contact"
        return f"{label} ({self.owner})"

    def save(self, *args, **kwargs):
        self.normalized_phone_number = normalize_phone_number(self.phone_number)
        self.normalized_email = normalize_email(self.email)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            fields = set(update_fields)
            fields.update({"normalized_phone_number", "normalized_email"})
            kwargs["update_fields"] = list(fields)
        super().save(*args, **kwargs)


class RecommendationActivity(models.Model):
    class EventType(models.TextChoices):
        PROFILE_VIEWED = "profile_viewed", "Profile Viewed"
        RECOMMENDATION_CLICKED = "recommendation_clicked", "Recommendation Clicked"
        SEARCH_RESULT_OPENED = "search_result_opened", "Search Result Opened"
        DESCRIPTION_QUERY_SUBMITTED = "description_query_submitted", "Description Query Submitted"
        FRIEND_REQUEST_SENT = "friend_request_sent", "Friend Request Sent"
        FRIEND_REQUEST_ACCEPTED = "friend_request_accepted", "Friend Request Accepted"
        FRIEND_REQUEST_DECLINED = "friend_request_declined", "Friend Request Declined"
        FRIEND_REQUEST_CANCELED = "friend_request_canceled", "Friend Request Canceled"
        CONTACT_MATCH_HIT = "contact_match_hit", "Contact Match Hit"
        CALL_TAPPED = "call_tapped", "Call Tapped"
        EMAIL_TAPPED = "email_tapped", "Email Tapped"

    class DiscoveryMode(models.TextChoices):
        FOR_YOU = "for_you", "For You"
        DESCRIBE_SOMEONE = "describe_someone", "Describe Someone"
        SEARCH = "search", "Search"
        DIRECT = "direct", "Direct"

    actor_profile = models.ForeignKey(
        AccountProfile,
        on_delete=models.CASCADE,
        related_name="recommendation_activities",
    )
    target_profile = models.ForeignKey(
        AccountProfile,
        on_delete=models.CASCADE,
        related_name="targeted_recommendation_activities",
        null=True,
        blank=True,
    )
    event_type = models.CharField(max_length=48, choices=EventType.choices)
    discovery_mode = models.CharField(
        max_length=32,
        choices=DiscoveryMode.choices,
        default=DiscoveryMode.DIRECT,
    )
    query_text = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    signal_strength = models.FloatField(default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.actor_profile} {self.event_type} {self.target_profile or ''}".strip()
