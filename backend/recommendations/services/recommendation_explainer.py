from __future__ import annotations

from recommendations.models import ElderProfile
from recommendations.services.compatibility_engine import compare_people


def explain_recommendation(
    *,
    query_profile: ElderProfile,
    candidate_profile: ElderProfile,
    graph_score: float,
    embedding_score: float,
    shared_neighbors: list[int] | None = None,
) -> dict:
    explanation = compare_people(
        query_profile.feature_vector,
        candidate_profile.feature_vector,
        left_confidence=query_profile.feature_confidence,
        right_confidence=candidate_profile.feature_confidence,
        graph_score=graph_score,
        embedding_score=embedding_score,
    )
    explanation["graph_evidence"] = {
        "shared_neighbor_count": len(shared_neighbors or []),
        "shared_neighbors": shared_neighbors or [],
    }
    explanation["possible_friction"] = [item["label"] for item in explanation["top_mismatches"][:3]]
    explanation["why_they_match"] = [item["label"] for item in explanation["top_matches"][:3]]
    return explanation
