from django.db import migrations, models


def cleanup_orphan_recommendation_activity(apps, schema_editor):
    AccountProfile = apps.get_model("accounts", "AccountProfile")
    RecommendationActivity = apps.get_model("accounts", "RecommendationActivity")

    valid_profile_ids = list(AccountProfile.objects.values_list("id", flat=True))
    RecommendationActivity.objects.exclude(actor_profile_id__in=valid_profile_ids).delete()
    RecommendationActivity.objects.exclude(target_profile_id__isnull=True).exclude(
        target_profile_id__in=valid_profile_ids
    ).update(target_profile_id=None)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_recommendationactivity"),
    ]

    operations = [
        migrations.RunPython(cleanup_orphan_recommendation_activity, migrations.RunPython.noop),
        migrations.AddField(
            model_name="accountprofile",
            name="home_lat",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="accountprofile",
            name="home_lng",
            field=models.FloatField(blank=True, null=True),
        ),
    ]