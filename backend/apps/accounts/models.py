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
