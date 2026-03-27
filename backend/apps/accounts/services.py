from __future__ import annotations

import math
import re
import secrets
from typing import Iterable

from django.contrib.auth.models import User
from django.db.models import Q

from recommendations.gat.feature_schema import get_default_feature_vector
from recommendations.models import ElderProfile, SocialEdge
from recommendations.services.compatibility_engine import compare_people, dominant_traits
from recommendations.services.feature_extraction import extract_feature_profile, extraction_to_vectors
from recommendations.services.profile_ingestion import (
    apply_interaction_signals,
    hydrate_profile_from_description,
)

from .models import (
    AccountProfile,
    AccountToken,
    FriendRequest,
    ImportedContact,
    RecommendationActivity,
    normalize_email,
)


def build_recommendation_username(user: User) -> str:
    return f"acct_{user.id}_{user.username}"[:150]


def ensure_account_profile(user: User) -> AccountProfile:
    profile, _ = AccountProfile.objects.get_or_create(
        user=user,
        defaults={"display_name": user.get_full_name().strip() or user.username},
    )
    return profile


def issue_token(user: User) -> AccountToken:
    token, _ = AccountToken.objects.update_or_create(
        user=user,
        defaults={"key": secrets.token_hex(24)},
    )
    return token


def profile_for_token(token_key: str | None) -> AccountProfile | None:
    if not token_key:
        return None
    token = (
        AccountToken.objects.select_related("user", "user__account_profile", "user__account_profile__elder_profile")
        .filter(key=token_key)
        .first()
    )
    if not token:
        return None
    return ensure_account_profile(token.user)


def sync_profile_to_recommendations(
    profile: AccountProfile,
    *,
    preserve_adaptation: bool = True,
) -> ElderProfile:
    recommendation_username = build_recommendation_username(profile.user)
    elder_profile = profile.elder_profile
    if elder_profile is None:
        elder_profile = ElderProfile.objects.create(
            username=recommendation_username,
            display_name=profile.display_name,
            description=profile.description or "",
        )
        profile.elder_profile = elder_profile
        profile.save(update_fields=["elder_profile"])
    else:
        elder_profile.username = recommendation_username
        elder_profile.display_name = profile.display_name
        elder_profile.description = profile.description or ""
        elder_profile.save(update_fields=["username", "display_name", "description", "updated_at"])

    hydrate_profile_from_description(
        profile=elder_profile,
        description=profile.description or "",
        clarification_answers=profile.onboarding_answers or {},
        vector_source="account_onboarding",
        preserve_adaptation=preserve_adaptation,
    )
    return elder_profile


def get_friend_request_between(
    left: AccountProfile,
    right: AccountProfile,
    *,
    statuses: Iterable[str] | None = None,
) -> FriendRequest | None:
    queryset = FriendRequest.objects.filter(
        Q(from_profile=left, to_profile=right) | Q(from_profile=right, to_profile=left)
    )
    if statuses:
        queryset = queryset.filter(status__in=list(statuses))
    return queryset.order_by("-updated_at", "-created_at").first()


def get_friendship_status(viewer: AccountProfile | None, target: AccountProfile) -> str:
    if viewer is None:
        return "anonymous"
    if viewer.pk == target.pk:
        return "self"
    request_obj = get_friend_request_between(
        viewer,
        target,
        statuses=[
            FriendRequest.Status.PENDING,
            FriendRequest.Status.ACCEPTED,
        ],
    )
    if not request_obj:
        return "none"
    if request_obj.status == FriendRequest.Status.ACCEPTED:
        return "accepted"
    if request_obj.from_profile_id == viewer.id:
        return "outgoing_pending"
    return "incoming_pending"


def are_friends(left: AccountProfile | None, right: AccountProfile) -> bool:
    if left is None:
        return False
    return get_friendship_status(left, right) == "accepted"


def can_view_email(viewer: AccountProfile | None, target: AccountProfile) -> bool:
    if viewer and viewer.pk == target.pk:
        return True
    return are_friends(viewer, target) and target.share_email_with_friends


def can_view_phone(viewer: AccountProfile | None, target: AccountProfile) -> bool:
    if viewer and viewer.pk == target.pk:
        return True
    return are_friends(viewer, target) and target.share_phone_with_friends


def build_match_summary(
    viewer: AccountProfile | None,
    target: AccountProfile,
    *,
    graph_score: float = 0.3,
) -> dict | None:
    if viewer is None or viewer.pk == target.pk:
        return None
    if not viewer.elder_profile_id or not target.elder_profile_id:
        return None
    return compare_people(
        viewer.elder_profile.feature_vector or {},
        target.elder_profile.feature_vector or {},
        left_confidence=viewer.elder_profile.feature_confidence or {},
        right_confidence=target.elder_profile.feature_confidence or {},
        graph_score=graph_score,
        embedding_score=graph_score,
    )


def build_top_traits(profile: AccountProfile) -> list[dict]:
    if not profile.elder_profile_id:
        return []
    return dominant_traits(
        profile.elder_profile.feature_vector or {},
        profile.elder_profile.feature_confidence or {},
    )


def refresh_social_edge_for_friendship(left: AccountProfile, right: AccountProfile) -> SocialEdge | None:
    left_elder = sync_profile_to_recommendations(left, preserve_adaptation=True)
    right_elder = sync_profile_to_recommendations(right, preserve_adaptation=True)
    comparison = compare_people(
        left_elder.feature_vector or {},
        right_elder.feature_vector or {},
        left_confidence=left_elder.feature_confidence or {},
        right_confidence=right_elder.feature_confidence or {},
    )
    edge_weight = max(0.75, float(comparison["compatibility_score"]))
    return SocialEdge.upsert(left_elder, right_elder, edge_weight)


def graph_scores_for_profile(viewer: AccountProfile) -> dict[int, float]:
    if not viewer.elder_profile_id:
        return {}

    try:
        from recommendations.gat.recommender import get_embedding_snapshot

        snapshot = get_embedding_snapshot()
        viewer_elder_id = viewer.elder_profile_id
        if viewer_elder_id not in snapshot["elder_ids"]:
            return {}

        query_index = snapshot["elder_ids"].index(viewer_elder_id)
        query_embedding = snapshot["embeddings"][query_index]
        scores: dict[int, float] = {}
        for index, elder_id in enumerate(snapshot["elder_ids"]):
            if elder_id == viewer_elder_id:
                continue
            similarity = float((query_embedding * snapshot["embeddings"][index]).sum().item())
            scores[int(elder_id)] = max(0.0, min(1.0, (similarity + 1.0) / 2.0))
        return scores
    except Exception:
        return {}


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def calibrate_match_percent(
    raw_score: float,
    *,
    feature_alignment: float = 0.5,
    certainty_score: float = 0.0,
    graph_score: float = 0.0,
) -> int:
    calibrated = 1.0 / (1.0 + math.exp(-6.0 * (raw_score - 0.45)))
    if feature_alignment >= 0.80 and certainty_score >= 0.55:
        calibrated += 0.08
    if graph_score >= 0.80:
        calibrated += 0.04
    return int(round(100 * _bounded(calibrated)))


def extract_query_profile(description: str) -> dict:
    try:
        extraction = extract_feature_profile(description)
        base_vector, effective_vector, confidence, evidence, _ = extraction_to_vectors(extraction)
    except ModuleNotFoundError as exc:
        if exc.name != "torch":
            raise
        base_vector = get_default_feature_vector()
        effective_vector = dict(base_vector)
        confidence = {"overall": 0.0, "source": "fallback_no_torch"}
        evidence = {"warning": "Torch is not installed; used default feature profile."}
    return {
        "description": description,
        "base_vector": base_vector,
        "feature_vector": effective_vector,
        "feature_confidence": confidence,
        "evidence": evidence,
    }


def keyword_overlap_score(left_text: str, right_text: str) -> float:
    token_pattern = re.compile(r"[a-z0-9]{4,}")
    left_tokens = set(token_pattern.findall((left_text or "").lower()))
    right_tokens = set(token_pattern.findall((right_text or "").lower()))
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    if union == 0:
        return 0.0
    return _bounded(overlap / union)


def _activity_signal_strength(event_type: str) -> float:
    strengths = {
        RecommendationActivity.EventType.PROFILE_VIEWED: 0.10,
        RecommendationActivity.EventType.RECOMMENDATION_CLICKED: 0.18,
        RecommendationActivity.EventType.SEARCH_RESULT_OPENED: 0.14,
        RecommendationActivity.EventType.DESCRIPTION_QUERY_SUBMITTED: 0.06,
        RecommendationActivity.EventType.FRIEND_REQUEST_SENT: 0.30,
        RecommendationActivity.EventType.FRIEND_REQUEST_ACCEPTED: 0.85,
        RecommendationActivity.EventType.FRIEND_REQUEST_DECLINED: 0.08,
        RecommendationActivity.EventType.FRIEND_REQUEST_CANCELED: 0.05,
        RecommendationActivity.EventType.CONTACT_MATCH_HIT: 0.24,
        RecommendationActivity.EventType.CALL_TAPPED: 0.50,
        RecommendationActivity.EventType.EMAIL_TAPPED: 0.42,
    }
    return strengths.get(event_type, 0.10)


def _update_edge_weight(
    left: AccountProfile,
    right: AccountProfile,
    *,
    weight: float,
    prefer_higher: bool = False,
) -> SocialEdge | None:
    if not left.elder_profile_id or not right.elder_profile_id:
        return None

    elder_a = left.elder_profile
    elder_b = right.elder_profile
    if elder_a.id > elder_b.id:
        elder_a, elder_b = elder_b, elder_a

    current = SocialEdge.objects.filter(elder_a=elder_a, elder_b=elder_b).first()
    next_weight = _bounded(weight)
    if current and prefer_higher:
        next_weight = max(float(current.gat_weight), next_weight)
    edge, _ = SocialEdge.objects.update_or_create(
        elder_a=elder_a,
        elder_b=elder_b,
        defaults={"gat_weight": next_weight},
    )
    return edge


def invalidate_gat_cache() -> None:
    try:
        from recommendations.gat.recommender import invalidate_model_cache

        invalidate_model_cache()
    except Exception:
        pass


def activity_affinity_for_pair(viewer: AccountProfile, target: AccountProfile) -> float:
    if viewer.pk == target.pk:
        return 1.0

    outgoing = RecommendationActivity.objects.filter(
        actor_profile=viewer,
        target_profile=target,
    )
    incoming = RecommendationActivity.objects.filter(
        actor_profile=target,
        target_profile=viewer,
        event_type=RecommendationActivity.EventType.FRIEND_REQUEST_ACCEPTED,
    )
    outgoing_strength = sum(float(item.signal_strength) for item in outgoing[:40])
    incoming_strength = sum(float(item.signal_strength) for item in incoming[:10])
    return _bounded((outgoing_strength + incoming_strength) / 2.0)


def _activity_signal_vector(
    actor: AccountProfile,
    target: AccountProfile,
    *,
    event_type: str,
    signal_strength: float,
) -> None:
    if not actor.elder_profile_id or not target.elder_profile_id:
        return

    positive_events = {
        RecommendationActivity.EventType.RECOMMENDATION_CLICKED,
        RecommendationActivity.EventType.SEARCH_RESULT_OPENED,
        RecommendationActivity.EventType.FRIEND_REQUEST_SENT,
        RecommendationActivity.EventType.FRIEND_REQUEST_ACCEPTED,
        RecommendationActivity.EventType.CALL_TAPPED,
        RecommendationActivity.EventType.EMAIL_TAPPED,
        RecommendationActivity.EventType.CONTACT_MATCH_HIT,
    }
    if event_type not in positive_events:
        return

    alpha = min(0.22, max(0.03, signal_strength * 0.18))
    apply_interaction_signals(
        profile=actor.elder_profile,
        signals=target.elder_profile.feature_vector or {},
        alpha=alpha,
    )


def record_recommendation_activity(
    actor: AccountProfile,
    *,
    event_type: str,
    target: AccountProfile | None = None,
    discovery_mode: str = RecommendationActivity.DiscoveryMode.DIRECT,
    query_text: str = "",
    metadata: dict | None = None,
    signal_strength: float | None = None,
) -> RecommendationActivity:
    strength = _bounded(signal_strength if signal_strength is not None else _activity_signal_strength(event_type))
    activity = RecommendationActivity.objects.create(
        actor_profile=actor,
        target_profile=target,
        event_type=event_type,
        discovery_mode=discovery_mode,
        query_text=query_text,
        metadata=metadata or {},
        signal_strength=strength,
    )

    if target is not None:
        _activity_signal_vector(actor, target, event_type=event_type, signal_strength=strength)

        if event_type == RecommendationActivity.EventType.FRIEND_REQUEST_ACCEPTED:
            _update_edge_weight(actor, target, weight=max(0.82, strength), prefer_higher=True)
        elif event_type in {
            RecommendationActivity.EventType.FRIEND_REQUEST_SENT,
            RecommendationActivity.EventType.RECOMMENDATION_CLICKED,
            RecommendationActivity.EventType.SEARCH_RESULT_OPENED,
            RecommendationActivity.EventType.CONTACT_MATCH_HIT,
            RecommendationActivity.EventType.CALL_TAPPED,
            RecommendationActivity.EventType.EMAIL_TAPPED,
        }:
            _update_edge_weight(actor, target, weight=0.35 + (0.5 * strength), prefer_higher=True)

    invalidate_gat_cache()
    return activity


def _match_row(
    *,
    viewer: AccountProfile,
    target: AccountProfile,
    matched_contact_ids: set[int],
    graph_score: float,
    activity_score: float,
    raw_score: float,
    discovery_mode: str,
    comparison: dict | None,
    query_comparison: dict | None = None,
) -> dict:
    row = _serialize_profile_for_recommendations(
        target,
        viewer=viewer,
        matched_contact_ids=matched_contact_ids,
        graph_score=graph_score,
    )
    score_breakdown = comparison.get("score_breakdown", {}) if comparison else {}
    feature_alignment = float(score_breakdown.get("feature_alignment", 0.5))
    certainty_score = float((comparison or {}).get("certainty_score", 0.0))
    row["discovery_mode"] = discovery_mode
    row["raw_score"] = round(_bounded(raw_score), 4)
    row["match_percent"] = calibrate_match_percent(
        raw_score,
        feature_alignment=feature_alignment,
        certainty_score=certainty_score,
        graph_score=graph_score,
    )
    row["score_components"] = {
        "graph_score": round(_bounded(graph_score), 4),
        "activity_score": round(_bounded(activity_score), 4),
        "requester_fit_score": round(_bounded((comparison or {}).get("compatibility_score", 0.0)), 4),
        "query_fit_score": round(_bounded((query_comparison or {}).get("compatibility_score", 0.0)), 4),
        "keyword_fit_score": round(
            _bounded(((query_comparison or {}).get("score_breakdown") or {}).get("keyword_fit_score", 0.0)),
            4,
        ),
        "feature_alignment": round(feature_alignment, 4),
        "certainty_score": round(_bounded(certainty_score), 4),
    }
    return row


def _serialize_profile_for_recommendations(
    target: AccountProfile,
    *,
    viewer: AccountProfile,
    matched_contact_ids: set[int],
    graph_score: float,
) -> dict:
    match_summary = build_match_summary(viewer, target, graph_score=graph_score)
    return {
        "user_id": target.user_id,
        "elder_profile_id": target.elder_profile_id,
        "username": target.user.username,
        "display_name": target.display_name,
        "description": target.description,
        "friend_status": get_friendship_status(viewer, target),
        "top_traits": build_top_traits(target),
        "matched_from_contacts": bool(matched_contact_ids and target.id in matched_contact_ids),
        "created_at": target.created_at.isoformat(),
        "updated_at": target.updated_at.isoformat(),
        "graph_score": graph_score,
        "contact_access": {
            "can_view_email": can_view_email(viewer, target),
            "can_view_phone": can_view_phone(viewer, target),
        },
        "email": target.user.email if can_view_email(viewer, target) else None,
        "phone_number": target.phone_number if can_view_phone(viewer, target) else None,
        "match_summary": {
            "compatibility_score": match_summary["compatibility_score"],
            "certainty_score": match_summary["certainty_score"],
            "friendship_summary": match_summary["friendship_summary"],
            "why_they_match": match_summary["why_they_match"],
            "possible_friction": match_summary["possible_friction"],
            "shared_interests": match_summary["shared_interests"],
        }
        if match_summary
        else None,
    }


def recommend_profiles_for_viewer(
    viewer: AccountProfile,
    *,
    query: str = "",
    limit: int = 12,
) -> list[dict]:
    queryset = AccountProfile.objects.select_related("user", "elder_profile").exclude(pk=viewer.pk)
    if query:
        queryset = queryset.filter(
            Q(display_name__icontains=query)
            | Q(user__username__icontains=query)
            | Q(user__email__icontains=query)
            | Q(description__icontains=query)
        )

    matched_contact_ids = matched_profile_ids_for_owner(viewer)
    graph_scores = graph_scores_for_profile(viewer)
    rows: list[dict] = []
    for target in queryset[: max(limit * 4, 30)]:
        comparison = build_match_summary(
            viewer,
            target,
            graph_score=float(graph_scores.get(target.elder_profile_id or -1, 0.0)),
        )
        requester_fit = float((comparison or {}).get("compatibility_score", 0.0))
        graph_score = float(graph_scores.get(target.elder_profile_id or -1, requester_fit))
        activity_score = activity_affinity_for_pair(viewer, target)
        raw_score = (0.50 * graph_score) + (0.35 * requester_fit) + (0.15 * activity_score)
        rows.append(
            _match_row(
                viewer=viewer,
                target=target,
                matched_contact_ids=matched_contact_ids,
                graph_score=graph_score,
                activity_score=activity_score,
                raw_score=raw_score,
                discovery_mode=RecommendationActivity.DiscoveryMode.FOR_YOU,
                comparison=comparison,
            )
        )

    rows.sort(
        key=lambda item: (
            -float(item["raw_score"]),
            -int(bool(item["matched_from_contacts"])),
            item["display_name"].lower(),
        )
    )
    return rows[:limit]


def recommend_profiles_for_description(
    viewer: AccountProfile,
    *,
    description: str,
    limit: int = 8,
) -> list[dict]:
    query_profile = extract_query_profile(description)
    matched_contact_ids = matched_profile_ids_for_owner(viewer)
    rows: list[dict] = []
    queryset = AccountProfile.objects.select_related("user", "elder_profile").exclude(pk=viewer.pk)
    for target in queryset[: max(limit * 5, 40)]:
        if not target.elder_profile_id:
            continue
        query_comparison = compare_people(
            query_profile["feature_vector"],
            target.elder_profile.feature_vector or {},
            left_confidence=query_profile["feature_confidence"],
            right_confidence=target.elder_profile.feature_confidence or {},
            graph_score=0.0,
            embedding_score=0.0,
        )
        requester_comparison = build_match_summary(viewer, target, graph_score=0.0)
        semantic_query_fit = float(query_comparison.get("compatibility_score", 0.0))
        keyword_fit = keyword_overlap_score(description, target.description)
        query_fit = (0.72 * semantic_query_fit) + (0.28 * keyword_fit)
        requester_fit = float((requester_comparison or {}).get("compatibility_score", 0.0))
        activity_score = activity_affinity_for_pair(viewer, target)
        raw_score = (0.75 * query_fit) + (0.15 * requester_fit) + (0.10 * activity_score)
        rows.append(
            _match_row(
                viewer=viewer,
                target=target,
                matched_contact_ids=matched_contact_ids,
                graph_score=0.0,
                activity_score=activity_score,
                raw_score=raw_score,
                discovery_mode=RecommendationActivity.DiscoveryMode.DESCRIBE_SOMEONE,
                comparison=requester_comparison,
                query_comparison={
                    **query_comparison,
                    "compatibility_score": query_fit,
                    "score_breakdown": {
                        **query_comparison.get("score_breakdown", {}),
                        "keyword_fit_score": round(keyword_fit, 4),
                        "semantic_query_fit_score": round(semantic_query_fit, 4),
                    },
                },
            )
        )

    rows.sort(key=lambda item: (-float(item["raw_score"]), item["display_name"].lower()))
    return rows[:limit]


def matched_profiles_for_contact(
    owner: AccountProfile,
    contact: ImportedContact,
) -> list[AccountProfile]:
    if not contact.normalized_phone_number and not contact.normalized_email:
        return []

    query = Q()
    if contact.normalized_phone_number:
        query |= Q(normalized_phone_number=contact.normalized_phone_number)
    if contact.normalized_email:
        query |= Q(user__email__iexact=normalize_email(contact.email))

    return list(
        AccountProfile.objects.select_related("user", "elder_profile")
        .filter(query)
        .exclude(pk=owner.pk)
        .distinct()
    )


def matched_profile_ids_for_owner(owner: AccountProfile) -> set[int]:
    phone_numbers = [
        item
        for item in owner.imported_contacts.values_list("normalized_phone_number", flat=True)
        if item
    ]
    emails = [
        item
        for item in owner.imported_contacts.values_list("normalized_email", flat=True)
        if item
    ]
    if not phone_numbers and not emails:
        return set()

    query = Q()
    if phone_numbers:
        query |= Q(normalized_phone_number__in=phone_numbers)
    if emails:
        query |= Q(user__email__in=emails)
    return set(
        AccountProfile.objects.filter(query).exclude(pk=owner.pk).values_list("id", flat=True)
    )
