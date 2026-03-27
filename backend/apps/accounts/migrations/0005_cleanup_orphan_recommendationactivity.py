from django.db import migrations


def cleanup_orphan_recommendation_activity(apps, schema_editor):
    AccountProfile = apps.get_model("accounts", "AccountProfile")
    RecommendationActivity = apps.get_model("accounts", "RecommendationActivity")

    valid_profile_ids = list(AccountProfile.objects.values_list("id", flat=True))

    # actor_profile is required; orphan rows must be removed.
    RecommendationActivity.objects.exclude(actor_profile_id__in=valid_profile_ids).delete()

    # target_profile is optional; orphan references can be nulled.
    RecommendationActivity.objects.exclude(target_profile_id__isnull=True).exclude(
        target_profile_id__in=valid_profile_ids
    ).update(target_profile_id=None)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_accountprofile_home_location"),
    ]

    operations = [
        migrations.RunPython(cleanup_orphan_recommendation_activity, migrations.RunPython.noop),
    ]
