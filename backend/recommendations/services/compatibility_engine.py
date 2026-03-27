from recommendations.gat.feature_schema import get_default_feature_vector, get_feature_details, get_feature_groups
from recommendations.gat.feature_schema import get_feature_names
import logging
import math


logger = logging.getLogger(__name__)


# Per-feature importance weights for matching.
# Higher = this feature matters more when judging compatibility.
_MATCH_WEIGHTS = {
    "extroversion": 0.85,
    "agreeableness": 1.0,
    "emotional_warmth": 1.15,
    "humor": 0.8,
    "positivity": 0.72,
    "patience": 0.95,
    "empathy": 1.1,
    "interest_family": 0.92,
    "interest_religion": 0.75,
    "verbosity": 0.78,
    "story_telling": 0.85,
    "prefers_small_groups": 1.05,
    "activity_level": 0.92,
    "nostalgia_index": 0.78,
    "spiritual_alignment": 0.82,
    "independence": 0.90,
    "directness": 0.75,
    "assertiveness": 0.70,
    "adaptability": 0.65,
    "interest_nature": 0.72,
    "interest_music": 0.72,
    "interest_arts": 0.68,
    "interest_sports": 0.72,
    "interest_technology": 0.65,
}

# Minimum strength-from-neutral for a feature to count as "opinionated"
_NEUTRAL_ZONE = 0.08

_GENERIC_TOPIC_FEATURES = {
    "interest_nature",
    "activity_level",
    "community_involvement",
    "hosting_preference",
}

_BEHAVIORAL_FEATURES = {
    "activity_level",
    "verbosity",
    "story_telling",
    "prefers_small_groups",
    "prefers_structure",
    "schedule_flexibility",
    "routine_orientation",
    "pace_of_speech",
    "listening_style",
    "question_asking",
    "conversation_depth",
    "responsiveness",
    "needs_personal_space",
    "adventure_comfort",
    "change_tolerance",
}


def _selected_features(features: list[str] | None = None) -> list[str]:
    return list(features or get_feature_names())


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _polarity(value: float) -> int:
    delta = float(value) - 0.5
    if abs(delta) < _NEUTRAL_ZONE:
        return 0
    return 1 if delta > 0 else -1


def _topic_features(selected_features: list[str]) -> list[str]:
    return [
        name
        for name in selected_features
        if name.startswith("interest_") or name in {"activity_level", "community_involvement", "hosting_preference"}
    ]


def _behavior_features(selected_features: list[str]) -> list[str]:
    return [name for name in selected_features if name in _BEHAVIORAL_FEATURES]


def _topic_similarity(
    vec_a: dict[str, float],
    vec_b: dict[str, float],
    *,
    features: list[str],
    confidence_a: dict[str, float] | None = None,
    confidence_b: dict[str, float] | None = None,
) -> tuple[float, dict]:
    defaults = get_default_feature_vector()
    confidence_a = confidence_a or {}
    confidence_b = confidence_b or {}

    weighted_sum = 0.0
    total_weight = 0.0
    feature_contributions: list[float] = []
    generic_overlap_weight = 0.0
    specific_overlap_weight = 0.0

    for feature_name in features:
        left = float(vec_a.get(feature_name, defaults.get(feature_name, 0.5)))
        right = float(vec_b.get(feature_name, defaults.get(feature_name, 0.5)))
        diff = left - right
        similarity = math.exp(-6.0 * (diff * diff))

        left_strength = abs(left - 0.5)
        right_strength = abs(right - 0.5)
        pair_strength = max(left_strength, right_strength)

        base_weight = float(_MATCH_WEIGHTS.get(feature_name, 0.60))
        if feature_name in _GENERIC_TOPIC_FEATURES:
            base_weight *= 0.70
        if pair_strength < _NEUTRAL_ZONE:
            base_weight *= 0.30

        shared_confidence = min(
            float(confidence_a.get(feature_name, 1.0)),
            float(confidence_b.get(feature_name, 1.0)),
        )
        effective_weight = base_weight * max(0.30, shared_confidence)
        contribution = similarity * effective_weight

        weighted_sum += contribution
        total_weight += effective_weight
        feature_contributions.append(max(0.0, contribution))

        if min(left_strength, right_strength) >= _NEUTRAL_ZONE:
            if feature_name in _GENERIC_TOPIC_FEATURES:
                generic_overlap_weight += effective_weight
            else:
                specific_overlap_weight += effective_weight

    topic_score = (weighted_sum / total_weight) if total_weight else 0.5

    overlap_weight = generic_overlap_weight + specific_overlap_weight
    generic_overlap_ratio = (generic_overlap_weight / overlap_weight) if overlap_weight > 0 else 0.0
    generic_suppressed = overlap_weight > 0 and generic_overlap_ratio >= 0.65
    if generic_suppressed:
        topic_score *= 0.5

    max_feature_share = 0.0
    total_contribution = sum(feature_contributions)
    if total_contribution > 0:
        max_feature_share = max(feature_contributions) / total_contribution
        if max_feature_share > 0.5:
            topic_score *= (0.5 / max_feature_share)

    return _bounded(topic_score), {
        "generic_overlap_ratio": round(_bounded(generic_overlap_ratio), 4),
        "generic_suppressed": generic_suppressed,
        "max_topic_feature_share": round(_bounded(max_feature_share), 4),
    }


def _preference_similarity(
    vec_a: dict[str, float],
    vec_b: dict[str, float],
    *,
    features: list[str],
    confidence_a: dict[str, float] | None = None,
    confidence_b: dict[str, float] | None = None,
) -> tuple[float, int, int]:
    defaults = get_default_feature_vector()
    confidence_a = confidence_a or {}
    confidence_b = confidence_b or {}

    weighted_sum = 0.0
    total_weight = 0.0
    contradictions = 0
    shared_polarity_topics = 0

    for feature_name in features:
        left = float(vec_a.get(feature_name, defaults.get(feature_name, 0.5)))
        right = float(vec_b.get(feature_name, defaults.get(feature_name, 0.5)))
        left_pol = _polarity(left)
        right_pol = _polarity(right)

        if left_pol == 0 and right_pol == 0:
            continue

        base_weight = float(_MATCH_WEIGHTS.get(feature_name, 0.70))
        shared_confidence = min(
            float(confidence_a.get(feature_name, 1.0)),
            float(confidence_b.get(feature_name, 1.0)),
        )
        weight = base_weight * max(0.30, shared_confidence)

        if left_pol != 0 and right_pol != 0:
            shared_polarity_topics += 1
            if left_pol == right_pol:
                intensity_gap = abs(abs(left - 0.5) - abs(right - 0.5))
                score = 1.0 - min(1.0, intensity_gap / 0.5)
            else:
                contradictions += 1
                score = 0.0
        else:
            score = 0.5

        weighted_sum += score * weight
        total_weight += weight

    if total_weight == 0:
        return 0.5, 0, 0
    return _bounded(weighted_sum / total_weight), contradictions, shared_polarity_topics


def _behavior_similarity(
    vec_a: dict[str, float],
    vec_b: dict[str, float],
    *,
    features: list[str],
    confidence_a: dict[str, float] | None = None,
    confidence_b: dict[str, float] | None = None,
) -> float:
    defaults = get_default_feature_vector()
    confidence_a = confidence_a or {}
    confidence_b = confidence_b or {}

    weighted_sum = 0.0
    total_weight = 0.0
    active_weight = 0.0
    for feature_name in features:
        left = float(vec_a.get(feature_name, defaults.get(feature_name, 0.5)))
        right = float(vec_b.get(feature_name, defaults.get(feature_name, 0.5)))
        diff = abs(left - right)
        left_strength = abs(left - 0.5)
        right_strength = abs(right - 0.5)
        is_active = max(left_strength, right_strength) >= _NEUTRAL_ZONE

        similarity = 1.0 - diff
        if not is_active:
            similarity = 0.5

        if _polarity(left) != 0 and _polarity(right) != 0 and _polarity(left) != _polarity(right):
            similarity *= 0.6

        shared_confidence = min(
            float(confidence_a.get(feature_name, 1.0)),
            float(confidence_b.get(feature_name, 1.0)),
        )
        weight = float(_MATCH_WEIGHTS.get(feature_name, 0.65)) * max(0.30, shared_confidence)
        if not is_active:
            weight *= 0.25

        weighted_sum += similarity * weight
        total_weight += weight
        if is_active:
            active_weight += weight

    if total_weight == 0:
        return 0.5
    behavior_raw = weighted_sum / total_weight
    signal_ratio = active_weight / total_weight if total_weight > 0 else 0.0
    behavior_score = (signal_ratio * behavior_raw) + ((1.0 - signal_ratio) * 0.5)
    return _bounded(behavior_score)


def _weighted_similarity(
    vec_a: dict[str, float],
    vec_b: dict[str, float],
    features: list[str],
    confidence_a: dict[str, float] | None = None,
    confidence_b: dict[str, float] | None = None,
) -> tuple[float, float, int, int]:
    """Compute weighted similarity with professional-grade calibration.

    Key improvements over the naive version:
    1. **Quadratic penalty**: sim = 1 - (a-b)^2.  Small diffs forgiven,
       big diffs punished hard (like how Instagram weights taste-vector
       distance with squared-error in their explore CF model).
    2. **Neutral de-weighting**: features where BOTH users are near 0.5
       (no opinion) contribute less — matching on "neither of us has an
       opinion" shouldn't boost compatibility.
    3. **Confidence as soft weight**: confidence modulates importance, but
       has a floor of 0.3 so missing data doesn't kill the comparison.
    """
    defaults = get_default_feature_vector()
    confidence_a = confidence_a or {}
    confidence_b = confidence_b or {}
    possible_weight = 0.0
    active_weight = 0.0
    total_score = 0.0
    aligned_feature_count = 0
    distinctive_aligned_feature_count = 0

    for feature_name in features:
        left = float(vec_a.get(feature_name, defaults.get(feature_name, 0.5)))
        right = float(vec_b.get(feature_name, defaults.get(feature_name, 0.5)))
        weight = float(_MATCH_WEIGHTS.get(feature_name, 0.55))

        # How opinionated are both users on this feature?
        left_strength = abs(left - 0.5)
        right_strength = abs(right - 0.5)

        # De-weight features where BOTH users are near-neutral.
        # If at least one user has a strong opinion, the feature should count.
        # If both are neutral, we ignore it to prevent "neutrality inflation".
        max_strength = max(left_strength, right_strength)
        joint_strength = min(left_strength, right_strength)
        neutrality_factor = 1.0
        if max_strength < _NEUTRAL_ZONE:
            # Drop-off for total boredom/neutrality
            neutrality_factor = 0.02 + 0.98 * (max_strength / _NEUTRAL_ZONE)

        effective_weight = weight * neutrality_factor
        possible_weight += effective_weight

        # Confidence as soft weight (floor at 0.3)
        shared_confidence = min(
            float(confidence_a.get(feature_name, 1.0)),
            float(confidence_b.get(feature_name, 1.0)),
        )
        effective_confidence = max(0.3, shared_confidence)
        active_weight += effective_weight * effective_confidence

        # Exponential similarity: penalizes large differences very heavily
        # (Gaussian-like curve). exp(-6.0 * diff^2) is very discriminating:
        # diff=0.15 is forgiven (~0.87), but diff=0.4 is crushed (~0.38).
        # diff=0.8 -> sim ~ 0.02
        diff = left - right
        similarity = math.exp(-6.0 * (diff * diff))

        total_score += similarity * effective_weight * effective_confidence

        # Track alignment stats
        linear_sim = 1.0 - abs(diff)
        if linear_sim >= 0.74:
            aligned_feature_count += 1
            if joint_strength >= _NEUTRAL_ZONE:
                distinctive_aligned_feature_count += 1

    similarity_score_raw = (total_score / active_weight) if active_weight else 0.5
    # Signal-strength damping: keep neutral-only comparisons near 0.5, but
    # do not over-compress genuinely aligned profiles with fewer explicit cues.
    signal_strength = min(1.0, active_weight / 4.5)
    similarity_score = (signal_strength * similarity_score_raw) + ((1.0 - signal_strength) * 0.5)

    certainty_score = (active_weight / possible_weight) if possible_weight else 0.0
    return similarity_score, certainty_score, aligned_feature_count, distinctive_aligned_feature_count


def dominant_traits(
    vec: dict[str, float],
    confidence: dict[str, float] | None = None,
    *,
    limit: int = 3,
    features: list[str] | None = None,
) -> list[dict]:
    defaults = get_default_feature_vector()
    metadata = {item["name"]: item for item in get_feature_details()}
    confidence = confidence or {}
    rows = []
    for feature_name in _selected_features(features):
        value = float(vec.get(feature_name, defaults.get(feature_name, 0.5)))
        feature_confidence = float(confidence.get(feature_name, 0.0))
        distance = abs(value - 0.5)
        if feature_confidence < 0.40 or distance < 0.06:
            continue
        rows.append(
            {
                "feature": feature_name,
                "label": metadata.get(feature_name, {}).get("label", feature_name),
                "value": round(value, 4),
                "confidence": round(feature_confidence, 4),
                "distance_from_neutral": round(distance, 4),
            }
        )
    rows.sort(key=lambda item: (item["distance_from_neutral"], item["confidence"]), reverse=True)
    return rows[:limit]


def _top_trait_alignment(
    vec_a: dict[str, float],
    vec_b: dict[str, float],
    confidence_a: dict[str, float] | None,
    confidence_b: dict[str, float] | None,
    *,
    descending: bool,
    features: list[str] | None = None,
) -> list[dict]:
    defaults = get_default_feature_vector()
    metadata = {item["name"]: item for item in get_feature_details()}
    confidence_a = confidence_a or {}
    confidence_b = confidence_b or {}
    rows = []
    for feature_name in _selected_features(features):
        left = float(vec_a.get(feature_name, defaults.get(feature_name, 0.5)))
        right = float(vec_b.get(feature_name, defaults.get(feature_name, 0.5)))
        shared_confidence = min(
            float(confidence_a.get(feature_name, 1.0)),
            float(confidence_b.get(feature_name, 1.0)),
        )
        similarity = 1.0 - abs(left - right)
        joint_strength = min(abs(left - 0.5), abs(right - 0.5))
        rows.append(
            {
                "feature": feature_name,
                "label": metadata.get(feature_name, {}).get("label", feature_name),
                "left": round(left, 4),
                "right": round(right, 4),
                "gap": round(abs(left - right), 4),
                "similarity": round(similarity, 4),
                "shared_confidence": round(shared_confidence, 4),
                "joint_strength": round(joint_strength, 4),
            }
        )
    rows.sort(
        key=lambda item: (
            item["similarity"] if descending else -item["gap"],
            item["joint_strength"],
            item["shared_confidence"],
        ),
        reverse=descending,
    )
    return rows[:5]


def _shared_interest_labels(
    vec_a: dict[str, float],
    vec_b: dict[str, float],
    confidence_a: dict[str, float] | None = None,
    confidence_b: dict[str, float] | None = None,
) -> list[str]:
    metadata = {item["name"]: item for item in get_feature_details()}
    return [
        metadata[feature_name]["label"]
        for feature_name in get_feature_groups().get("Interests & Activities", [])
        if vec_a.get(feature_name, 0.5) >= 0.62
        and vec_b.get(feature_name, 0.5) >= 0.62
    ]


def _friendship_summary(
    score_breakdown: dict,
    top_matches: list[dict],
    top_mismatches: list[dict],
    shared_interests: list[str],
) -> str:
    overall = score_breakdown["overall"]
    if overall >= 0.78:
        tone = "Very strong friendship potential"
    elif overall >= 0.62:
        tone = "Promising friendship potential"
    elif overall >= 0.45:
        tone = "Moderate compatibility — needs common ground"
    elif overall >= 0.30:
        tone = "Low compatibility — different personalities"
    else:
        tone = "Very low compatibility — significant personality gaps"

    match_labels = ", ".join(item["label"] for item in top_matches[:2]) or "general warmth"
    mismatch_labels = ", ".join(item["label"] for item in top_mismatches[:2]) or "no major mismatches"
    interests = ", ".join(shared_interests[:3]) or "few obvious shared interests yet"
    return (
        f"{tone}. They align most around {match_labels}, share interest signals in {interests}, "
        f"and may need to navigate differences around {mismatch_labels}."
    )


def _semantic_intent_alignment(
    left_intents: list[dict] | None,
    right_intents: list[dict] | None,
) -> tuple[float, int, int, int, list[str]]:
    left_intents = left_intents or []
    right_intents = right_intents or []
    if not left_intents or not right_intents:
        return 0.5, 0, 0, 0, []

    left_map: dict[str, float] = {}
    right_map: dict[str, float] = {}
    for item in left_intents:
        obj = str(item.get("object", "")).strip().lower()
        if not obj:
            continue
        left_map[obj] = float(item.get("polarity", 0.0))
    for item in right_intents:
        obj = str(item.get("object", "")).strip().lower()
        if not obj:
            continue
        right_map[obj] = float(item.get("polarity", 0.0))

    overlap = sorted(set(left_map.keys()) & set(right_map.keys()))
    if not overlap:
        return 0.5, 0, 0, 0, []

    aligned = 0
    contradictions = 0
    for obj in overlap:
        l = left_map[obj]
        r = right_map[obj]
        if abs(l) < 0.5 or abs(r) < 0.5:
            continue
        if l * r < 0:
            contradictions += 1
        else:
            aligned += 1

    compared = max(1, aligned + contradictions)
    base = aligned / compared
    contradiction_ratio = contradictions / compared
    adjusted = _bounded(base - (0.75 * contradiction_ratio))
    return adjusted, contradictions, aligned, len(overlap), overlap


def compare_people(
    left: dict[str, float],
    right: dict[str, float],
    *,
    left_confidence: dict[str, float] | None = None,
    right_confidence: dict[str, float] | None = None,
    graph_score: float = 0.3,
    embedding_score: float = 0.3,
    features: list[str] | None = None,
    left_intents: list[dict] | None = None,
    right_intents: list[dict] | None = None,
) -> dict:
    selected_features = _selected_features(features)
    groups = get_feature_groups()
    defaults = get_default_feature_vector()

    global_alignment, _, _, _ = _weighted_similarity(
        left,
        right,
        selected_features,
        left_confidence,
        right_confidence,
    )
    topic_features = [f for f in _topic_features(selected_features) if f in selected_features]
    behavior_features = _behavior_features(selected_features)
    if not topic_features:
        topic_features = [f for f in groups.get("Interests & Activities", []) if f in selected_features]
    if not behavior_features:
        behavior_features = [
            f
            for f in (
                groups.get("Communication", []) + groups.get("Social Preferences", [])
            )
            if f in selected_features
        ]

    topic_similarity, topic_meta = _topic_similarity(
        left,
        right,
        features=topic_features,
        confidence_a=left_confidence,
        confidence_b=right_confidence,
    )

    active_topic_count = 0
    for feature_name in topic_features:
        left_value = float(left.get(feature_name, defaults.get(feature_name, 0.5)))
        right_value = float(right.get(feature_name, defaults.get(feature_name, 0.5)))
        if max(abs(left_value - 0.5), abs(right_value - 0.5)) >= _NEUTRAL_ZONE:
            active_topic_count += 1
    if active_topic_count == 0:
        topic_similarity = global_alignment
    preference_similarity, contradiction_count, shared_polarity_topics = _preference_similarity(
        left,
        right,
        features=topic_features,
        confidence_a=left_confidence,
        confidence_b=right_confidence,
    )
    behavior_similarity = _behavior_similarity(
        left,
        right,
        features=behavior_features,
        confidence_a=left_confidence,
        confidence_b=right_confidence,
    )

    intent_similarity, intent_contradictions, intent_aligned, intent_overlap_count, intent_overlap_items = _semantic_intent_alignment(
        left_intents,
        right_intents,
    )

    overall = (0.42 * topic_similarity) + (0.28 * preference_similarity) + (0.15 * behavior_similarity) + (0.15 * intent_similarity)
    semantic_polarity_factor = 1.0
    if intent_overlap_count > 0 and intent_contradictions > 0:
        intent_contradiction_ratio = intent_contradictions / max(1, intent_overlap_count)
        semantic_polarity_factor = max(0.15, 1.0 - (0.90 * intent_contradiction_ratio))
        overall *= semantic_polarity_factor

    # Hard guard rails for explicit opposite preference statements.
    if intent_overlap_count > 0 and intent_contradictions > 0 and intent_aligned == 0:
        overall = min(overall, 0.28)
    elif intent_overlap_count > 0 and intent_contradictions == 0 and intent_aligned >= 1:
        overall = max(overall, 0.72)

    contradiction_penalty_factor = 1.0
    if contradiction_count > 0:
        if shared_polarity_topics > 0 and contradiction_count >= shared_polarity_topics:
            contradiction_penalty_factor = 0.45
        else:
            contradiction_ratio = contradiction_count / max(1, shared_polarity_topics)
            contradiction_penalty_factor = max(0.65, 1.0 - (0.40 * contradiction_ratio))
        overall *= contradiction_penalty_factor
    elif global_alignment >= 0.88:
        overall += 0.06
    overall = _bounded(overall)

    feature_alignment = topic_similarity
    interest_overlap = topic_similarity
    communication_fit = behavior_similarity
    social_style = behavior_similarity
    certainty_score = max(
        0.0,
        min(
            1.0,
            (
                float((left_confidence or {}).get("overall", 1.0))
                + float((right_confidence or {}).get("overall", 1.0))
            )
            / 2.0,
        ),
    )
    certainty_adj = 1.0
    graph_affinity = feature_alignment

    aligned_feature_count = sum(
        1
        for item in _top_trait_alignment(
            left,
            right,
            left_confidence,
            right_confidence,
            descending=True,
            features=selected_features,
        )
        if item["similarity"] >= 0.74
    )
    distinctive_aligned_feature_count = sum(
        1
        for item in _top_trait_alignment(
            left,
            right,
            left_confidence,
            right_confidence,
            descending=True,
            features=selected_features,
        )
        if item["similarity"] >= 0.74 and item["joint_strength"] >= _NEUTRAL_ZONE
    )

    top_matches = _top_trait_alignment(
        left, right, left_confidence, right_confidence,
        descending=True, features=selected_features,
    )
    top_mismatches = sorted(
        _top_trait_alignment(
            left, right, left_confidence, right_confidence,
            descending=False, features=selected_features,
        ),
        key=lambda item: item["gap"],
        reverse=True,
    )[:4]
    shared_interests = _shared_interest_labels(left, right, left_confidence, right_confidence)

    breakdown = {
        "overall": round(overall, 4),
        "feature_alignment": round(feature_alignment, 4),
        "interest_overlap": round(interest_overlap, 4),
        "communication_fit": round(communication_fit, 4),
        "social_style": round(social_style, 4),
        "topic_similarity": round(topic_similarity, 4),
        "preference_similarity": round(preference_similarity, 4),
        "behavior_similarity": round(behavior_similarity, 4),
        "intent_similarity": round(intent_similarity, 4),
        "intent_overlap_count": intent_overlap_count,
        "intent_overlap_items": intent_overlap_items,
        "intent_contradictions": intent_contradictions,
        "semantic_polarity_factor": round(semantic_polarity_factor, 4),
        "contradiction_count": contradiction_count,
        "shared_polarity_topics": shared_polarity_topics,
        "contradiction_penalty_factor": round(contradiction_penalty_factor, 4),
        "generic_overlap_ratio": topic_meta["generic_overlap_ratio"],
        "generic_topic_suppressed": topic_meta["generic_suppressed"],
        "max_topic_feature_share": topic_meta["max_topic_feature_share"],
        "certainty_score": round(max(0.0, min(1.0, certainty_score)), 4),
        "certainty_adj": round(max(0.0, min(1.0, certainty_adj)), 4),
        "graph_affinity": round(max(0.0, min(1.0, graph_affinity)), 4),
        "embedding_score": round(max(0.0, min(1.0, embedding_score)), 4),
        "graph_score": round(max(0.0, min(1.0, graph_score)), 4),
        "aligned_feature_count": aligned_feature_count,
        "distinctive_aligned_feature_count": distinctive_aligned_feature_count,
    }

    logger.info(
        "compatibility.semantic_debug intent_similarity=%.3f intent_contradictions=%s intent_overlap=%s raw_topic=%.3f raw_pref=%.3f final=%.3f",
        intent_similarity,
        intent_contradictions,
        intent_overlap_items,
        topic_similarity,
        preference_similarity,
        breakdown["overall"],
    )

    return {
        "compatibility_score": breakdown["overall"],
        "score_breakdown": breakdown,
        "top_matches": top_matches,
        "top_mismatches": top_mismatches,
        "shared_interests": shared_interests,
        "certainty_score": breakdown["certainty_score"],
        "friendship_summary": _friendship_summary(breakdown, top_matches, top_mismatches, shared_interests),
        "why_they_match": [item["label"] for item in top_matches if item["similarity"] >= 0.80],
        "possible_friction": [item["label"] for item in top_mismatches if item["gap"] >= 0.25],
        "graph_evidence": {
            "embedding_score": breakdown["embedding_score"],
            "graph_score": breakdown["graph_score"],
        },
    }
