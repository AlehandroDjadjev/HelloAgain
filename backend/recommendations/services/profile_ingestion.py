from __future__ import annotations

from datetime import datetime, timezone

from recommendations.gat.feature_updater import apply_manual_override, update_feature_vector
from recommendations.gat.feature_schema import get_default_feature_vector
from recommendations.models import ElderProfile


def hydrate_profile_from_description(
    *,
    profile: ElderProfile,
    description: str,
    manual_overrides: dict[str, float] | None = None,
    clarification_answers: dict[str, str] | None = None,
    vector_source: str = "description_hybrid",
    preserve_adaptation: bool = False,
) -> ElderProfile:
    try:
        from recommendations.services.feature_extraction import (
            extract_feature_profile,
            extraction_to_vectors,
        )

        extraction = extract_feature_profile(
            description,
            manual_overrides=manual_overrides,
            clarification_answers=clarification_answers,
        )
        base_vector, effective_vector, confidence, evidence, _ = extraction_to_vectors(extraction)
    except ModuleNotFoundError as exc:
        if exc.name != "torch":
            raise

        # Keep onboarding functional even when torch-based extraction isn't installed.
        base_vector = get_default_feature_vector()
        effective_vector = dict(base_vector)
        confidence = {"overall": 0.0, "source": "fallback_no_torch"}
        evidence = {
            "warning": "Torch is not installed; used default feature profile.",
        }
    current_effective = profile.feature_vector or effective_vector
    adapted_vector = (
        current_effective
        if preserve_adaptation and profile.feature_vector
        else dict(effective_vector)
    )

    if manual_overrides:
        base_vector = apply_manual_override(base_vector, manual_overrides)
        adapted_vector = apply_manual_override(adapted_vector, manual_overrides)

    profile.description = description
    profile.base_feature_vector = base_vector
    profile.adapted_feature_vector = adapted_vector
    profile.feature_vector = adapted_vector
    profile.feature_confidence = confidence
    profile.extraction_evidence = {
        **evidence,
        "clarification_answers": clarification_answers or {},
    }
    profile.manual_overrides = manual_overrides or {}
    profile.vector_source = vector_source
    profile.feature_vector_version = max(1, int(profile.feature_vector_version or 0) + 1)
    profile.extraction_timestamp = datetime.now(timezone.utc)
    profile.save()
    return profile


def apply_interaction_signals(
    *,
    profile: ElderProfile,
    signals: dict[str, float],
    alpha: float,
) -> ElderProfile:
    adapted = update_feature_vector(profile.adapted_feature_vector or profile.feature_vector, signals, alpha=alpha)
    profile.adapted_feature_vector = adapted
    profile.feature_vector = dict(adapted)
    profile.save(update_fields=["adapted_feature_vector", "feature_vector", "updated_at"])
    return profile
