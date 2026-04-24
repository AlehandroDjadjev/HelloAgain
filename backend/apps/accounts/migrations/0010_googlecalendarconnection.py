from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0009_accountprofile_whiteboard_state_connectionthread_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="GoogleCalendarConnection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("google_email", models.EmailField(blank=True, max_length=254)),
                ("access_token", models.TextField(blank=True)),
                ("refresh_token", models.TextField(blank=True)),
                ("token_uri", models.URLField(default="https://oauth2.googleapis.com/token")),
                ("scopes", models.JSONField(blank=True, default=list)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "profile",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="google_calendar_connection",
                        to="accounts.accountprofile",
                    ),
                ),
            ],
            options={"ordering": ["-updated_at", "-id"]},
        ),
    ]
