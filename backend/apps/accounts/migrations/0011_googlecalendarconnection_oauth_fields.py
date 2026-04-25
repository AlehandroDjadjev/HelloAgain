from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0010_googlecalendarconnection"),
    ]

    operations = [
        migrations.AddField(
            model_name="googlecalendarconnection",
            name="connected_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="googlecalendarconnection",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="googlecalendarconnection",
            name="oauth_state",
            field=models.CharField(blank=True, db_index=True, max_length=120),
        ),
    ]
