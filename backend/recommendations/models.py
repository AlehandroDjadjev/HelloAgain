from django.db import models

from .gat.feature_schema import get_default_feature_vector


class ElderProfile(models.Model):
    """
    Compatibility-focused profile with both extracted and adapted vectors.
    """

    username = models.CharField(max_length=150, unique=True)
    display_name = models.CharField(max_length=120, blank=True)
    description = models.TextField(blank=True)
    feature_vector = models.JSONField(default=dict)
    base_feature_vector = models.JSONField(default=dict)
    adapted_feature_vector = models.JSONField(default=dict)
    manual_overrides = models.JSONField(default=dict)
    feature_confidence = models.JSONField(default=dict)
    extraction_evidence = models.JSONField(default=dict)
    vector_source = models.CharField(max_length=64, default="description_hybrid")
    feature_vector_version = models.PositiveIntegerField(default=1)
    extraction_timestamp = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.display_name or self.username

    def _sync_vector(self, payload: dict | None, defaults: dict[str, float]) -> dict[str, float]:
        if not payload:
            return dict(defaults)
        synced = dict(payload)
        for feature_name, default_value in defaults.items():
            synced.setdefault(feature_name, default_value)
        return synced

    def save(self, *args, **kwargs):
        defaults = get_default_feature_vector()
        self.base_feature_vector = self._sync_vector(self.base_feature_vector, defaults)
        self.adapted_feature_vector = self._sync_vector(
            self.adapted_feature_vector or self.base_feature_vector,
            defaults,
        )
        self.feature_vector = self._sync_vector(
            self.feature_vector or self.adapted_feature_vector,
            defaults,
        )
        self.manual_overrides = {
            feature_name: float(value)
            for feature_name, value in (self.manual_overrides or {}).items()
            if feature_name in defaults
        }
        self.feature_confidence = {
            feature_name: float(max(0.0, min(1.0, value)))
            for feature_name, value in (self.feature_confidence or {}).items()
            if feature_name in defaults
        }
        self.extraction_evidence = self.extraction_evidence or {}
        super().save(*args, **kwargs)


class SocialEdge(models.Model):
    elder_a = models.ForeignKey(
        ElderProfile, on_delete=models.CASCADE, related_name="edges_as_a"
    )
    elder_b = models.ForeignKey(
        ElderProfile, on_delete=models.CASCADE, related_name="edges_as_b"
    )
    gat_weight = models.FloatField(default=0.5)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("elder_a", "elder_b")]
        ordering = ["-gat_weight"]

    def __str__(self):
        return f"{self.elder_a} <-> {self.elder_b} ({self.gat_weight:.3f})"

    @classmethod
    def upsert(cls, profile_a: "ElderProfile", profile_b: "ElderProfile", weight: float):
        if profile_a.id > profile_b.id:
            profile_a, profile_b = profile_b, profile_a
        obj, _ = cls.objects.update_or_create(
            elder_a=profile_a,
            elder_b=profile_b,
            defaults={"gat_weight": weight},
        )
        return obj


class TrainingRun(models.Model):
    mode = models.CharField(max_length=32, default="baseline")
    model_family = models.CharField(max_length=64, default="legacy_gat")
    status = models.CharField(max_length=32, default="completed")
    confidence = models.CharField(max_length=16, default="high")
    promotion_status = models.CharField(max_length=32, default="kept_candidate")
    promoted = models.BooleanField(default=False)
    config = models.JSONField(default=dict)
    metrics = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.model_family} {self.mode} #{self.id}"
