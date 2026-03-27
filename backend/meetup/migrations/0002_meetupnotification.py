from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0006_merge_20260327_1405"),
        ("meetup", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="MeetupNotification",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "notification_type",
                    models.CharField(
                        choices=[
                            ("invite_request", "Invite Request"),
                            ("invite_accepted", "Invite Accepted"),
                            ("invite_declined", "Invite Declined"),
                            ("invite_canceled", "Invite Canceled"),
                            ("reminder_20m", "Reminder 20 Minutes"),
                        ],
                        max_length=32,
                    ),
                ),
                ("title", models.CharField(max_length=180)),
                ("body", models.TextField()),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("scheduled_for", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("read_at", models.DateTimeField(blank=True, null=True)),
                (
                    "invite",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notifications",
                        to="meetup.meetupinvite",
                    ),
                ),
                (
                    "recipient_profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="meetup_notifications",
                        to="accounts.accountprofile",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
    ]
