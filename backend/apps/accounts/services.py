from __future__ import annotations

import secrets
from typing import Iterable

from django.contrib.auth.models import User
from django.db.models import Q

from recommendations.models import ElderProfile, SocialEdge
from recommendations.services.compatibility_engine import compare_people, dominant_traits
from recommendations.services.profile_ingestion import hydrate_profile_from_description

from .models import AccountProfile, AccountToken, FriendRequest, ImportedContact, normalize_email


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
