from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agent_policy", "0002_systempolicyconfig_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="policydecisionrecord",
            name="action_type",
            field=models.CharField(
                blank=True,
                default="",
                help_text="ActionType value for step-level policy checks.",
                max_length=64,
            ),
        ),
    ]
