from django.db import models


class MeetupInvite(models.Model):
	class Status(models.TextChoices):
		PENDING = "pending", "Pending"
		ACCEPTED = "accepted", "Accepted"
		DECLINED = "declined", "Declined"
		CANCELED = "canceled", "Canceled"

	requester_profile = models.ForeignKey(
		"accounts.AccountProfile",
		on_delete=models.CASCADE,
		related_name="sent_meetup_invites",
	)
	invited_profile = models.ForeignKey(
		"accounts.AccountProfile",
		on_delete=models.CASCADE,
		related_name="received_meetup_invites",
	)
	status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
	proposed_time = models.DateTimeField()
	place_name = models.CharField(max_length=255)
	place_lat = models.FloatField()
	place_lng = models.FloatField()
	center_lat = models.FloatField()
	center_lng = models.FloatField()
	weather = models.CharField(max_length=64, blank=True)
	temperature = models.FloatField(null=True, blank=True)
	score = models.FloatField(default=0.0)
	payload = models.JSONField(default=dict, blank=True)
	responded_at = models.DateTimeField(null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ["-created_at", "-id"]

	def __str__(self):
		return f"Meetup {self.requester_profile_id}->{self.invited_profile_id} ({self.status})"


class MeetupNotification(models.Model):
	class Type(models.TextChoices):
		INVITE_REQUEST = "invite_request", "Invite Request"
		INVITE_ACCEPTED = "invite_accepted", "Invite Accepted"
		INVITE_DECLINED = "invite_declined", "Invite Declined"
		INVITE_CANCELED = "invite_canceled", "Invite Canceled"
		REMINDER_20M = "reminder_20m", "Reminder 20 Minutes"

	recipient_profile = models.ForeignKey(
		"accounts.AccountProfile",
		on_delete=models.CASCADE,
		related_name="meetup_notifications",
	)
	invite = models.ForeignKey(
		MeetupInvite,
		on_delete=models.CASCADE,
		related_name="notifications",
		null=True,
		blank=True,
	)
	notification_type = models.CharField(max_length=32, choices=Type.choices)
	title = models.CharField(max_length=180)
	body = models.TextField()
	payload = models.JSONField(default=dict, blank=True)
	scheduled_for = models.DateTimeField(null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	read_at = models.DateTimeField(null=True, blank=True)

	class Meta:
		ordering = ["-created_at", "-id"]

	def __str__(self):
		return f"MeetupNotification {self.notification_type} -> {self.recipient_profile_id}"
