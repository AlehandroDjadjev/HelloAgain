from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Action",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=160, unique=True)),
                ("description", models.TextField(blank=True, default="")),
                ("base_summary", models.TextField(blank=True, default="")),
                ("created_from_prompt", models.TextField(blank=True, default="")),
                ("hit_count", models.IntegerField(default=0)),
                ("helpful_count", models.IntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="Attribute",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="MainUserProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(default="main_user", max_length=120, unique=True)),
                ("description", models.TextField(blank=True, default="")),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="ActionAttributeScore",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("score", models.FloatField(default=0.0)),
                ("contribution_count", models.IntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("action", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="attribute_scores", to="controller.action")),
                ("attribute", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="action_scores", to="controller.attribute")),
            ],
            options={"unique_together": {("action", "attribute")}},
        ),
        migrations.CreateModel(
            name="UserActionEdge",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("score", models.FloatField(default=0.0)),
                ("confidence", models.FloatField(default=1.0)),
                ("last_signal_kind", models.CharField(choices=[("neutral", "neutral"), ("desire", "desire"), ("positive", "positive"), ("negative", "negative"), ("memory", "memory"), ("fetch", "fetch")], default="neutral", max_length=32)),
                ("touch_count", models.IntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("action", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="user_edges", to="controller.action")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="action_edges", to="controller.mainuserprofile")),
            ],
            options={"unique_together": {("user", "action")}},
        ),
        migrations.CreateModel(
            name="UserAttributeScore",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("score", models.FloatField(default=0.0)),
                ("confidence", models.FloatField(default=1.0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("attribute", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="user_scores", to="controller.attribute")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="attribute_scores", to="controller.mainuserprofile")),
            ],
            options={"unique_together": {("user", "attribute")}},
        ),
    ]
