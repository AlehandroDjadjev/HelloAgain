from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("accounts", "0004_accountprofile_home_location"),
    ]

    operations = [
        migrations.CreateModel(
            name="MeetupInvite",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("pending", "Pending"), ("accepted", "Accepted"), ("declined", "Declined"), ("canceled", "Canceled")], default="pending", max_length=16)),
                ("proposed_time", models.DateTimeField()),
                ("place_name", models.CharField(max_length=255)),
                ("place_lat", models.FloatField()),
                ("place_lng", models.FloatField()),
                ("center_lat", models.FloatField()),
                ("center_lng", models.FloatField()),
                ("weather", models.CharField(blank=True, max_length=64)),
                ("temperature", models.FloatField(blank=True, null=True)),
                ("score", models.FloatField(default=0.0)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("responded_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "invited_profile",
                    models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="received_meetup_invites", to="accounts.accountprofile"),
                ),
                (
                    "requester_profile",
                    models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="sent_meetup_invites", to="accounts.accountprofile"),
                ),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
    ]