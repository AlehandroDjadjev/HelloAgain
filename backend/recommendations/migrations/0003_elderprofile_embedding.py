from django.db import migrations
from pgvector.django import VectorExtension, VectorField

from recommendations.gat.feature_schema import FEATURE_NAMES, get_default_feature_vector


def backfill_profile_embeddings(apps, schema_editor):
    ElderProfile = apps.get_model("recommendations", "ElderProfile")
    defaults = get_default_feature_vector()

    for profile in ElderProfile.objects.all().iterator():
        payload = profile.feature_vector or profile.adapted_feature_vector or profile.base_feature_vector or {}
        embedding = [
            float(payload.get(feature_name, defaults.get(feature_name, 0.5)))
            for feature_name in FEATURE_NAMES
        ]
        profile.embedding = embedding
        profile.save(update_fields=["embedding"])


class Migration(migrations.Migration):

    dependencies = [
        ("recommendations", "0002_trainingrun_elderprofile_adapted_feature_vector_and_more"),
    ]

    operations = [
        VectorExtension(),
        migrations.AddField(
            model_name="elderprofile",
            name="embedding",
            field=VectorField(blank=True, dimensions=64, null=True),
        ),
        migrations.RunPython(backfill_profile_embeddings, migrations.RunPython.noop),
    ]
