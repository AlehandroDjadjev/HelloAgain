from recommendations.gat.feature_schema import get_default_feature_vector, get_feature_details, get_feature_groups
from recommendations.gat.feature_schema import get_feature_names
import math


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


def _selected_features(features: list[str] | None = None) -> list[str]:
    return list(features or get_feature_names())


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
    # Signal-strength damping: if there is very little opinionated weight,
    # pull the score toward 0.5 (neutral) rather than letting a single tiny
    # match define the whole score. active_weight ~1.6 is All-Neutral.
    signal_strength = min(1.0, active_weight / 8.0)
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


def compare_people(
    left: dict[str, float],
    right: dict[str, float],
    *,
    left_confidence: dict[str, float] | None = None,
    right_confidence: dict[str, float] | None = None,
    graph_score: float = 0.3,
    embedding_score: float = 0.3,
    features: list[str] | None = None,
) -> dict:
    selected_features = _selected_features(features)
    groups = get_feature_groups()

    feature_alignment, certainty_score, aligned_feature_count, distinctive_aligned_feature_count = _weighted_similarity(
        left, right, selected_features, left_confidence, right_confidence,
    )
    interest_overlap, _, _, _ = _weighted_similarity(
        left, right,
        [f for f in groups.get("Interests & Activities", []) if f in selected_features],
        left_confidence, right_confidence,
    )
    communication_fit, _, _, _ = _weighted_similarity(
        left, right,
        [f for f in groups.get("Communication", []) if f in selected_features],
        left_confidence, right_confidence,
    )
    social_style, _, _, _ = _weighted_similarity(
        left, right,
        [f for f in groups.get("Social Preferences", []) if f in selected_features],
        left_confidence, right_confidence,
    )

    # Blend: feature-level analysis + graph/embedding signals
    # Shifted most weight (0.75) to core alignment for professional objectivity.
    blended_score = (
        (0.75 * feature_alignment)
        + (0.08 * interest_overlap)
        + (0.04 * communication_fit)
        + (0.04 * social_style)
        + (0.06 * embedding_score)
        + (0.03 * graph_score)
    )

    # Soft certainty adjustment: floor at 0.75 so uncertain data
    # reduces score by at most 25%, not 75% like the old version.
    certainty_adj = 0.75 + (0.25 * certainty_score)
    overall = blended_score * certainty_adj

    graph_affinity = feature_alignment

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
        "overall": round(max(0.0, min(1.0, overall)), 4),
        "feature_alignment": round(feature_alignment, 4),
        "interest_overlap": round(interest_overlap, 4),
        "communication_fit": round(communication_fit, 4),
        "social_style": round(social_style, 4),
        "certainty_score": round(max(0.0, min(1.0, certainty_score)), 4),
        "certainty_adj": round(max(0.0, min(1.0, certainty_adj)), 4),
        "graph_affinity": round(max(0.0, min(1.0, graph_affinity)), 4),
        "embedding_score": round(max(0.0, min(1.0, embedding_score)), 4),
        "graph_score": round(max(0.0, min(1.0, graph_score)), 4),
        "aligned_feature_count": aligned_feature_count,
        "distinctive_aligned_feature_count": distinctive_aligned_feature_count,
    }
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
