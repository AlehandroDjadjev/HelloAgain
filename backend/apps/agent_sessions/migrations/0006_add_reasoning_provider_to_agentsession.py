from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agent_sessions", "0005_add_intent_fields_to_agentsession"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentsession",
            name="reasoning_provider",
            field=models.CharField(
                choices=[("local", "Local Model"), ("openai", "OpenAI API")],
                default="local",
                help_text="Which reasoning backend should power intent parsing and step selection.",
                max_length=16,
            ),
        ),
    ]
