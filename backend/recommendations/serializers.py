from rest_framework import serializers

from .gat.feature_schema import (
    get_default_feature_vector,
    get_feature_details,
    get_feature_groups,
    get_feature_names,
)
from .models import ElderProfile, TrainingRun
from .services.compatibility_engine import dominant_traits


class ElderProfileSerializer(serializers.ModelSerializer):
    feature_vector = serializers.SerializerMethodField()
    base_feature_vector = serializers.SerializerMethodField()
    adapted_feature_vector = serializers.SerializerMethodField()
    feature_groups = serializers.SerializerMethodField()
    feature_details = serializers.SerializerMethodField()
    dominant_traits = serializers.SerializerMethodField()

    class Meta:
        model = ElderProfile
        fields = [
            "id",
            "username",
            "display_name",
            "description",
            "feature_vector",
            "base_feature_vector",
            "adapted_feature_vector",
            "feature_confidence",
            "extraction_evidence",
            "manual_overrides",
            "vector_source",
            "feature_vector_version",
            "extraction_timestamp",
            "feature_groups",
            "feature_details",
            "dominant_traits",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def _serialize_vector(self, payload: dict) -> dict[str, float]:
        defaults = get_default_feature_vector()
        return {
            name: round(float(payload.get(name, defaults.get(name, 0.5))), 4)
            for name in get_feature_names()
        }

    def get_feature_vector(self, obj):
        return self._serialize_vector(obj.feature_vector or {})

    def get_base_feature_vector(self, obj):
        return self._serialize_vector(obj.base_feature_vector or {})

    def get_adapted_feature_vector(self, obj):
        return self._serialize_vector(obj.adapted_feature_vector or {})

    def get_feature_groups(self, obj):
        grouped = {}
        vector = obj.feature_vector or {}
        defaults = get_default_feature_vector()
        for group_name, feature_names in get_feature_groups().items():
            grouped[group_name] = {
                feature_name: round(float(vector.get(feature_name, defaults.get(feature_name, 0.5))), 4)
                for feature_name in feature_names
            }
        return grouped

    def get_feature_details(self, obj):
        return get_feature_details()

    def get_dominant_traits(self, obj):
        return dominant_traits(obj.feature_vector or {}, obj.feature_confidence or {})


class ElderProfileCreateSerializer(serializers.ModelSerializer):
    username = serializers.CharField(required=False, allow_blank=True)
    manual_overrides = serializers.DictField(
        child=serializers.FloatField(min_value=0.0, max_value=1.0),
        required=False,
    )
    clarification_answers = serializers.DictField(
        child=serializers.CharField(),
        required=False,
    )

    class Meta:
        model = ElderProfile
        fields = ["username", "display_name", "description", "manual_overrides", "clarification_answers"]


class FeatureUpdateSerializer(serializers.Serializer):
    elder_id = serializers.IntegerField()
    signals = serializers.DictField(child=serializers.FloatField(min_value=0.0, max_value=1.0))
    alpha = serializers.FloatField(min_value=0.01, max_value=1.0, default=0.15, required=False)


class RecommendationSerializer(serializers.Serializer):
    elder_id = serializers.IntegerField()
    name = serializers.CharField()
    score = serializers.FloatField()
    raw_similarity = serializers.FloatField(required=False)
    feature_similarity = serializers.FloatField(required=False)
    compatibility_score = serializers.FloatField(required=False)
    graph_score = serializers.FloatField(required=False)
    certainty_score = serializers.FloatField(required=False)
    why_they_match = serializers.ListField(child=serializers.CharField(), required=False)
    possible_friction = serializers.ListField(child=serializers.CharField(), required=False)
    shared_interests = serializers.ListField(child=serializers.CharField(), required=False)
    friendship_summary = serializers.CharField(required=False)


class TrainingRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrainingRun
        fields = [
            "id",
            "mode",
            "model_family",
            "status",
            "confidence",
            "promotion_status",
            "promoted",
            "config",
            "metrics",
            "created_at",
            "updated_at",
        ]
