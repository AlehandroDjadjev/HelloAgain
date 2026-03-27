from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_recommendationactivity"),
    ]

    operations = [
        migrations.AddField(
            model_name="accountprofile",
            name="dynamic_profile_summary",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="accountprofile",
            name="profile_notes",
            field=models.TextField(blank=True),
        ),
    ]
