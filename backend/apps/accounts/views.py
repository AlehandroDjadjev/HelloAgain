from __future__ import annotations

import json

from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from recommendations.services.feature_extraction import extract_feature_profile, extraction_to_vectors
from recommendations.services.intake_clarification import (
    needs_clarification,
    select_clarification_questions,
)

from .models import AccountProfile, FriendRequest, ImportedContact, normalize_email
from .serializers import (
    AccountProfileUpdateSerializer,
    ContactsImportSerializer,
    FriendRequestCreateSerializer,
    FriendRequestResponseSerializer,
    LoginSerializer,
    RegisterSerializer,
)
from .services import (
    build_match_summary,
    build_top_traits,
    can_view_email,
    can_view_phone,
    ensure_account_profile,
    get_friend_request_between,
    get_friendship_status,
    graph_scores_for_profile,
    issue_token,
    matched_profile_ids_for_owner,
    matched_profiles_for_contact,
    profile_for_token,
    refresh_social_edge_for_friendship,
    sync_profile_to_recommendations,
)


def _json_ok(data: dict, status: int = 200) -> JsonResponse:
    return JsonResponse(data, status=status)


def _json_error(message: str, status: int = 400, code: str | None = None, details: dict | None = None) -> JsonResponse:
    payload = {"status": "error", "message": message}
    if code:
        payload["code"] = code
    if details:
        payload["details"] = details
    return JsonResponse(payload, status=status)


def _parse_body(request) -> dict:
    if not request.body:
        return {}
    return json.loads(request.body)


def _json_validation_error(
    errors: dict,
    *,
    message: str = "Please correct the highlighted fields.",
    status: int = 400,
) -> JsonResponse:
    return JsonResponse(
        {
            "status": "error",
            "message": message,
            "errors": errors,
        },
        status=status,
    )


def _get_token_from_request(request) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("token "):
        return header.split(None, 1)[1].strip()
    return None


def _require_profile(request) -> AccountProfile | JsonResponse:
    profile = profile_for_token(_get_token_from_request(request))
    if profile is None:
        return _json_error("Authentication required.", status=401, code="AUTH_REQUIRED")
    return profile


def _serialize_profile(
    target: AccountProfile,
    *,
    viewer: AccountProfile | None,
    matched_contact_ids: set[int] | None = None,
    graph_score: float = 0.3,
) -> dict:
    friend_status = get_friendship_status(viewer, target)
    match_summary = build_match_summary(viewer, target, graph_score=graph_score)
    data = {
        "user_id": target.user_id,
        "elder_profile_id": target.elder_profile_id,
        "username": target.user.username,
        "display_name": target.display_name,
        "description": target.description,
        "friend_status": friend_status,
        "top_traits": build_top_traits(target),
        "matched_from_contacts": bool(matched_contact_ids and target.id in matched_contact_ids),
        "created_at": target.created_at.isoformat(),
        "updated_at": target.updated_at.isoformat(),
        "graph_score": graph_score if viewer and viewer.pk != target.pk else None,
        "contact_access": {
            "can_view_email": can_view_email(viewer, target),
            "can_view_phone": can_view_phone(viewer, target),
        },
        "email": target.user.email if can_view_email(viewer, target) else None,
        "phone_number": target.phone_number if can_view_phone(viewer, target) else None,
        "match_summary": None,
    }
    if match_summary:
        data["match_summary"] = {
            "compatibility_score": match_summary["compatibility_score"],
            "certainty_score": match_summary["certainty_score"],
            "friendship_summary": match_summary["friendship_summary"],
            "why_they_match": match_summary["why_they_match"],
            "possible_friction": match_summary["possible_friction"],
            "shared_interests": match_summary["shared_interests"],
        }
    if viewer and viewer.pk == target.pk:
        data.update(
            {
                "contacts_permission_granted": target.contacts_permission_granted,
                "contacts_permission_granted_at": (
                    target.contacts_permission_granted_at.isoformat()
                    if target.contacts_permission_granted_at
                    else None
                ),
                "share_phone_with_friends": target.share_phone_with_friends,
                "share_email_with_friends": target.share_email_with_friends,
                "onboarding_answers": target.onboarding_answers,
            }
        )
    return data


def _serialize_friend_request(
    request_obj: FriendRequest,
    *,
    viewer: AccountProfile,
    matched_contact_ids: set[int] | None = None,
) -> dict:
    counterparty = (
        request_obj.to_profile
        if request_obj.from_profile_id == viewer.id
        else request_obj.from_profile
    )
    direction = "outgoing" if request_obj.from_profile_id == viewer.id else "incoming"
    return {
        "id": request_obj.id,
        "status": request_obj.status,
        "message": request_obj.message,
        "direction": direction,
        "created_at": request_obj.created_at.isoformat(),
        "updated_at": request_obj.updated_at.isoformat(),
        "responded_at": request_obj.responded_at.isoformat() if request_obj.responded_at else None,
        "counterparty": _serialize_profile(
            counterparty,
            viewer=viewer,
            matched_contact_ids=matched_contact_ids,
        ),
    }


def _extract_contact_value(raw: object) -> str:
    if isinstance(raw, list):
        return str(raw[0]).strip() if raw else ""
    if raw is None:
        return ""
    return str(raw).strip()


def _profile_sort_key(item: dict) -> tuple:
    summary = item.get("match_summary") or {}
    return (
        -int(bool(item.get("matched_from_contacts"))),
        -float(summary.get("compatibility_score", 0.0)),
        item.get("display_name", "").lower(),
    )


def _serialize_profile_list(
    profiles: list[AccountProfile],
    *,
    viewer: AccountProfile,
    matched_contact_ids: set[int],
    graph_scores: dict[int, float],
) -> list[dict]:
    rows = [
        _serialize_profile(
            item,
            viewer=viewer,
            matched_contact_ids=matched_contact_ids,
            graph_score=float(graph_scores.get(item.elder_profile_id or -1, 0.3)),
        )
        for item in profiles
    ]
    rows.sort(key=_profile_sort_key)
    return rows


@csrf_exempt
@require_http_methods(["POST"])
def onboarding_questions_preview(request):
    try:
        body = _parse_body(request)
    except json.JSONDecodeError:
        return _json_error("Invalid JSON body.")

    description = str(body.get("description") or "").strip()
    onboarding_answers = body.get("onboarding_answers") or {}

    extraction = extract_feature_profile(
        description,
        clarification_answers=onboarding_answers,
    )
    _, _, confidence, _, _ = extraction_to_vectors(extraction)
    needs_more, sufficiency_score = needs_clarification(confidence)
    questions = select_clarification_questions(confidence, limit=4)

    return _json_ok(
        {
            "status": "needs_clarification" if needs_more else "ready",
            "sufficiency_score": sufficiency_score,
            "questions": questions,
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def register_view(request):
    try:
        body = _parse_body(request)
    except json.JSONDecodeError:
        return _json_error("Invalid JSON body.")

    serializer = RegisterSerializer(data=body)
    if not serializer.is_valid():
        return _json_validation_error(serializer.errors, message="Sign up failed.")

    data = serializer.validated_data
    with transaction.atomic():
        user = User.objects.create_user(
            username=data["username"],
            email=normalize_email(data["email"]),
            password=data["password"],
        )
        profile = AccountProfile.objects.create(
            user=user,
            display_name=data.get("display_name") or data["username"],
            phone_number=data.get("phone_number", ""),
            description=data.get("description", ""),
            onboarding_answers=data.get("onboarding_answers", {}),
            contacts_permission_granted=data.get("contacts_permission_granted", False),
            share_phone_with_friends=data.get("share_phone_with_friends", True),
            share_email_with_friends=data.get("share_email_with_friends", True),
        )
        sync_profile_to_recommendations(profile, preserve_adaptation=False)
        token = issue_token(user)

    return _json_ok(
        {
            "status": "success",
            "token": token.key,
            "profile": _serialize_profile(profile, viewer=profile),
        },
        status=201,
    )


@csrf_exempt
@require_http_methods(["POST"])
def login_view(request):
    try:
        body = _parse_body(request)
    except json.JSONDecodeError:
        return _json_error("Invalid JSON body.")

    serializer = LoginSerializer(data=body)
    if not serializer.is_valid():
        return _json_validation_error(serializer.errors, message="Login failed.")

    identifier = serializer.validated_data["identifier"].strip()
    password = serializer.validated_data["password"]
    user = User.objects.filter(Q(username__iexact=identifier) | Q(email__iexact=identifier)).first()
    if not user:
        return _json_error("Invalid credentials.", status=401, code="INVALID_CREDENTIALS")

    authenticated = authenticate(request, username=user.username, password=password)
    if authenticated is None:
        return _json_error("Invalid credentials.", status=401, code="INVALID_CREDENTIALS")

    profile = ensure_account_profile(authenticated)
    if not profile.elder_profile_id:
        sync_profile_to_recommendations(profile, preserve_adaptation=False)
    token = issue_token(authenticated)

    return _json_ok(
        {
            "status": "success",
            "token": token.key,
            "profile": _serialize_profile(profile, viewer=profile),
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def logout_view(request):
    profile = _require_profile(request)
    if isinstance(profile, JsonResponse):
        return profile

    if hasattr(profile.user, "account_token"):
        profile.user.account_token.delete()
    return _json_ok({"status": "success"})


@csrf_exempt
@require_http_methods(["GET", "PATCH"])
def me_view(request):
    profile = _require_profile(request)
    if isinstance(profile, JsonResponse):
        return profile

    if request.method == "GET":
        return _json_ok({"profile": _serialize_profile(profile, viewer=profile)})

    try:
        body = _parse_body(request)
    except json.JSONDecodeError:
        return _json_error("Invalid JSON body.")

    serializer = AccountProfileUpdateSerializer(data=body, partial=True)
    if not serializer.is_valid():
        return _json_validation_error(serializer.errors, message="Profile update failed.")

    changed_profile_fields = False
    for field, value in serializer.validated_data.items():
        setattr(profile, field, value)
        if field in {"display_name", "description", "onboarding_answers"}:
            changed_profile_fields = True
    profile.save()

    if changed_profile_fields or "phone_number" in serializer.validated_data:
        sync_profile_to_recommendations(profile, preserve_adaptation=True)

    return _json_ok({"profile": _serialize_profile(profile, viewer=profile)})


@require_http_methods(["GET"])
def user_detail(request, user_id: int):
    viewer = _require_profile(request)
    if isinstance(viewer, JsonResponse):
        return viewer

    target = get_object_or_404(
        AccountProfile.objects.select_related("user", "elder_profile"),
        user_id=user_id,
    )
    matched_contact_ids = matched_profile_ids_for_owner(viewer)
    graph_scores = graph_scores_for_profile(viewer)
    return _json_ok(
        {
            "profile": _serialize_profile(
                target,
                viewer=viewer,
                matched_contact_ids=matched_contact_ids,
                graph_score=float(graph_scores.get(target.elder_profile_id or -1, 0.3)),
            )
        }
    )


@require_http_methods(["GET"])
def search_users(request):
    viewer = _require_profile(request)
    if isinstance(viewer, JsonResponse):
        return viewer

    query = request.GET.get("q", "").strip()
    queryset = AccountProfile.objects.select_related("user", "elder_profile").exclude(pk=viewer.pk)
    if query:
        queryset = queryset.filter(
            Q(display_name__icontains=query)
            | Q(user__username__icontains=query)
            | Q(user__email__icontains=query)
            | Q(description__icontains=query)
        )
    profiles = list(queryset[:25])
    matched_contact_ids = matched_profile_ids_for_owner(viewer)
    graph_scores = graph_scores_for_profile(viewer)
    rows = _serialize_profile_list(
        profiles,
        viewer=viewer,
        matched_contact_ids=matched_contact_ids,
        graph_scores=graph_scores,
    )

    return _json_ok({"count": len(rows), "results": rows})


@require_http_methods(["GET"])
def discovery_feed(request):
    viewer = _require_profile(request)
    if isinstance(viewer, JsonResponse):
        return viewer

    query = request.GET.get("q", "").strip()
    queryset = AccountProfile.objects.select_related("user", "elder_profile").exclude(pk=viewer.pk)

    recommended_ids: list[int] = []
    if viewer.elder_profile_id:
        try:
            from recommendations.gat.recommender import get_embedding_snapshot

            snapshot = get_embedding_snapshot()
            if viewer.elder_profile_id in snapshot["elder_ids"]:
                query_index = snapshot["elder_ids"].index(viewer.elder_profile_id)
                query_embedding = snapshot["embeddings"][query_index]
                ranked_pairs = []
                for index, elder_id in enumerate(snapshot["elder_ids"]):
                    if elder_id == viewer.elder_profile_id:
                        continue
                    similarity = float((query_embedding * snapshot["embeddings"][index]).sum().item())
                    graph_score = max(0.0, min(1.0, (similarity + 1.0) / 2.0))
                    ranked_pairs.append((int(elder_id), graph_score))
                ranked_pairs.sort(key=lambda item: item[1], reverse=True)
                recommended_ids = [elder_id for elder_id, _ in ranked_pairs]
        except Exception:
            recommended_ids = []

    if query:
        queryset = queryset.filter(
            Q(display_name__icontains=query)
            | Q(user__username__icontains=query)
            | Q(user__email__icontains=query)
            | Q(description__icontains=query)
        )

    profiles = list(queryset[:40])
    if recommended_ids:
        order_map = {elder_id: index for index, elder_id in enumerate(recommended_ids)}
        profiles.sort(key=lambda item: order_map.get(item.elder_profile_id or -1, 10_000))

    matched_contact_ids = matched_profile_ids_for_owner(viewer)
    graph_scores = graph_scores_for_profile(viewer)
    rows = _serialize_profile_list(
        profiles,
        viewer=viewer,
        matched_contact_ids=matched_contact_ids,
        graph_scores=graph_scores,
    )
    return _json_ok({"count": len(rows), "results": rows})


@require_http_methods(["GET"])
def friends_list(request):
    viewer = _require_profile(request)
    if isinstance(viewer, JsonResponse):
        return viewer

    accepted_requests = FriendRequest.objects.select_related(
        "from_profile__user",
        "from_profile__elder_profile",
        "to_profile__user",
        "to_profile__elder_profile",
    ).filter(
        status=FriendRequest.Status.ACCEPTED
    ).filter(
        Q(from_profile=viewer) | Q(to_profile=viewer)
    )
    matched_contact_ids = matched_profile_ids_for_owner(viewer)
    friends = []
    for request_obj in accepted_requests:
        friend = request_obj.to_profile if request_obj.from_profile_id == viewer.id else request_obj.from_profile
        friends.append(
            _serialize_profile(
                friend,
                viewer=viewer,
                matched_contact_ids=matched_contact_ids,
            )
        )
    friends.sort(key=lambda item: item["display_name"].lower())
    return _json_ok({"count": len(friends), "friends": friends})


@csrf_exempt
@require_http_methods(["GET", "POST"])
def friend_requests_collection(request):
    viewer = _require_profile(request)
    if isinstance(viewer, JsonResponse):
        return viewer

    matched_contact_ids = matched_profile_ids_for_owner(viewer)
    if request.method == "GET":
        incoming = [
            _serialize_friend_request(item, viewer=viewer, matched_contact_ids=matched_contact_ids)
            for item in FriendRequest.objects.select_related(
                "from_profile__user",
                "from_profile__elder_profile",
                "to_profile__user",
                "to_profile__elder_profile",
            ).filter(to_profile=viewer, status=FriendRequest.Status.PENDING)
        ]
        outgoing = [
            _serialize_friend_request(item, viewer=viewer, matched_contact_ids=matched_contact_ids)
            for item in FriendRequest.objects.select_related(
                "from_profile__user",
                "from_profile__elder_profile",
                "to_profile__user",
                "to_profile__elder_profile",
            ).filter(from_profile=viewer, status=FriendRequest.Status.PENDING)
        ]
        return _json_ok({"incoming": incoming, "outgoing": outgoing})

    try:
        body = _parse_body(request)
    except json.JSONDecodeError:
        return _json_error("Invalid JSON body.")

    serializer = FriendRequestCreateSerializer(data=body)
    if not serializer.is_valid():
        return _json_validation_error(serializer.errors, message="Friend request failed.")

    target_user_id = serializer.validated_data.get("target_user_id")
    target_username = serializer.validated_data.get("target_username")
    message = serializer.validated_data.get("message", "")

    target = AccountProfile.objects.select_related("user", "elder_profile").filter(
        Q(user_id=target_user_id) | Q(user__username__iexact=target_username or "")
    ).first()
    if not target:
        return _json_error("Target user not found.", status=404, code="USER_NOT_FOUND")
    if target.pk == viewer.pk:
        return _json_error("You cannot send a friend request to yourself.", status=400)

    existing = get_friend_request_between(
        viewer,
        target,
        statuses=[
            FriendRequest.Status.PENDING,
            FriendRequest.Status.ACCEPTED,
            FriendRequest.Status.DECLINED,
            FriendRequest.Status.CANCELED,
        ],
    )
    if existing and existing.status == FriendRequest.Status.ACCEPTED:
        return _json_error("You are already friends.", status=400, code="ALREADY_FRIENDS")
    if existing and existing.status == FriendRequest.Status.PENDING:
        if existing.from_profile_id == viewer.id:
            return _json_error("Friend request already sent.", status=400, code="REQUEST_ALREADY_SENT")
        return _json_error(
            "This user has already sent you a request.",
            status=400,
            code="INCOMING_REQUEST_EXISTS",
            details={"request_id": existing.id},
        )

    if existing and existing.from_profile_id == viewer.id:
        existing.status = FriendRequest.Status.PENDING
        existing.message = message
        existing.responded_at = None
        existing.save(update_fields=["status", "message", "responded_at", "updated_at"])
        request_obj = existing
    else:
        request_obj = FriendRequest.objects.create(
            from_profile=viewer,
            to_profile=target,
            status=FriendRequest.Status.PENDING,
            message=message,
        )

    return _json_ok(
        {
            "status": "success",
            "friend_request": _serialize_friend_request(
                request_obj,
                viewer=viewer,
                matched_contact_ids=matched_contact_ids,
            ),
        },
        status=201,
    )


@csrf_exempt
@require_http_methods(["POST"])
def respond_to_friend_request(request, request_id: int):
    viewer = _require_profile(request)
    if isinstance(viewer, JsonResponse):
        return viewer

    try:
        body = _parse_body(request)
    except json.JSONDecodeError:
        return _json_error("Invalid JSON body.")

    serializer = FriendRequestResponseSerializer(data=body)
    if not serializer.is_valid():
        return _json_validation_error(serializer.errors, message="Friend request update failed.")

    request_obj = get_object_or_404(
        FriendRequest.objects.select_related(
            "from_profile__user",
            "from_profile__elder_profile",
            "to_profile__user",
            "to_profile__elder_profile",
        ),
        pk=request_id,
    )
    if request_obj.status != FriendRequest.Status.PENDING:
        return _json_error("This request is no longer pending.", status=400, code="REQUEST_CLOSED")

    action = serializer.validated_data["action"]
    if action in {"accept", "decline"} and request_obj.to_profile_id != viewer.id:
        return _json_error("Only the recipient can respond to this request.", status=403)
    if action == "cancel" and request_obj.from_profile_id != viewer.id:
        return _json_error("Only the sender can cancel this request.", status=403)

    status_map = {
        "accept": FriendRequest.Status.ACCEPTED,
        "decline": FriendRequest.Status.DECLINED,
        "cancel": FriendRequest.Status.CANCELED,
    }
    request_obj.status = status_map[action]
    request_obj.responded_at = timezone.now()
    request_obj.save(update_fields=["status", "responded_at", "updated_at"])

    if action == "accept":
        refresh_social_edge_for_friendship(request_obj.from_profile, request_obj.to_profile)

    matched_contact_ids = matched_profile_ids_for_owner(viewer)
    return _json_ok(
        {
            "status": "success",
            "friend_request": _serialize_friend_request(
                request_obj,
                viewer=viewer,
                matched_contact_ids=matched_contact_ids,
            ),
        }
    )


@require_http_methods(["GET"])
def contacts_collection(request):
    viewer = _require_profile(request)
    if isinstance(viewer, JsonResponse):
        return viewer

    contacts = []
    matched_contact_ids = matched_profile_ids_for_owner(viewer)
    for contact in viewer.imported_contacts.all():
        contacts.append(
            {
                "id": contact.id,
                "full_name": contact.full_name,
                "phone_number": contact.phone_number,
                "email": contact.email,
                "source": contact.source,
                "created_at": contact.created_at.isoformat(),
                "matched_users": [
                    _serialize_profile(
                        match,
                        viewer=viewer,
                        matched_contact_ids=matched_contact_ids,
                    )
                    for match in matched_profiles_for_contact(viewer, contact)
                ],
            }
        )
    return _json_ok({"count": len(contacts), "contacts": contacts})


@csrf_exempt
@require_http_methods(["POST"])
def import_contacts_view(request):
    viewer = _require_profile(request)
    if isinstance(viewer, JsonResponse):
        return viewer
    if not viewer.contacts_permission_granted:
        return _json_error(
            "Contacts permission must be granted before importing contacts.",
            status=400,
            code="CONTACT_PERMISSION_REQUIRED",
        )

    try:
        body = _parse_body(request)
    except json.JSONDecodeError:
        return _json_error("Invalid JSON body.")

    serializer = ContactsImportSerializer(data=body)
    if not serializer.is_valid():
        return _json_validation_error(serializer.errors, message="Contact import failed.")

    replace_existing = serializer.validated_data.get("replace_existing", True)
    source = serializer.validated_data.get("source") or "manual"
    contacts_payload = serializer.validated_data.get("contacts", [])

    if replace_existing:
        viewer.imported_contacts.all().delete()

    created_contacts = []
    for raw_contact in contacts_payload:
        name = _extract_contact_value(raw_contact.get("full_name") or raw_contact.get("name"))
        phone_number = _extract_contact_value(
            raw_contact.get("phone_number") or raw_contact.get("phone") or raw_contact.get("tel")
        )
        email = _extract_contact_value(raw_contact.get("email") or raw_contact.get("emails"))
        if not name and not phone_number and not email:
            continue
        created_contacts.append(
            ImportedContact.objects.create(
                owner=viewer,
                full_name=name,
                phone_number=phone_number,
                email=normalize_email(email),
                source=source,
            )
        )

    matched_contact_ids = matched_profile_ids_for_owner(viewer)
    contacts = [
        {
            "id": contact.id,
            "full_name": contact.full_name,
            "phone_number": contact.phone_number,
            "email": contact.email,
            "source": contact.source,
            "matched_users": [
                _serialize_profile(
                    match,
                    viewer=viewer,
                    matched_contact_ids=matched_contact_ids,
                )
                for match in matched_profiles_for_contact(viewer, contact)
            ],
        }
        for contact in created_contacts
    ]
    return _json_ok(
        {
            "status": "success",
            "imported_count": len(created_contacts),
            "matched_user_count": len(matched_contact_ids),
            "contacts": contacts,
        }
    )
