from datetime import datetime, timedelta

from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import FriendRequest, RecommendationActivity
from apps.accounts.services import (
    get_friendship_status,
    profile_for_token,
    record_recommendation_activity,
)

from .models import MeetupInvite, MeetupNotification
from .services import get_best_meetup_spot, get_central_point, get_ranked_meetup_spots


def _json_ok(data: dict, status_code: int = 200) -> JsonResponse:
    return JsonResponse(data, status=status_code)


def _json_error(message: str, status_code: int = 400, code: str | None = None) -> JsonResponse:
    payload = {"status": "error", "message": message}
    if code:
        payload["code"] = code
    return JsonResponse(payload, status=status_code)


def _parse_body(request) -> dict:
    import json

    if not request.body:
        return {}
    return json.loads(request.body)


def _token_from_request(request) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("token "):
        return header.split(None, 1)[1].strip()
    return None


def _require_profile(request):
    profile = profile_for_token(_token_from_request(request))
    if profile is None:
        return _json_error("Authentication required.", status_code=401, code="AUTH_REQUIRED")
    return profile


def _invite_payload(invite: MeetupInvite, viewer_profile_id: int) -> dict:
    local_time = timezone.localtime(invite.proposed_time)
    weekday_map = {
        0: "понеделник",
        1: "вторник",
        2: "сряда",
        3: "четвъртък",
        4: "петък",
        5: "събота",
        6: "неделя",
    }
    meeting_day = weekday_map.get(local_time.weekday(), "")
    meeting_date = local_time.strftime("%d.%m.%Y")
    meeting_time = local_time.strftime("%H:%M")

    direction = "outgoing" if invite.requester_profile_id == viewer_profile_id else "incoming"
    return {
        "id": invite.id,
        "status": invite.status,
        "direction": direction,
        "requester_user_id": invite.requester_profile.user_id,
        "requester_display_name": invite.requester_profile.display_name,
        "invited_user_id": invite.invited_profile.user_id,
        "invited_display_name": invite.invited_profile.display_name,
        "proposed_time": invite.proposed_time.isoformat(),
        "place_name": invite.place_name,
        "place_lat": invite.place_lat,
        "place_lng": invite.place_lng,
        "center_lat": invite.center_lat,
        "center_lng": invite.center_lng,
        "weather": invite.weather,
        "temperature": invite.temperature,
        "score": invite.score,
        "meeting_day_bg": meeting_day,
        "meeting_date_bg": meeting_date,
        "meeting_time_bg": meeting_time,
        "meeting_when_bg": f"{meeting_day}, {meeting_date} в {meeting_time}",
        "payload": invite.payload,
        "responded_at": invite.responded_at.isoformat() if invite.responded_at else None,
        "created_at": invite.created_at.isoformat(),
        "updated_at": invite.updated_at.isoformat(),
    }


def _notification_payload(notification: MeetupNotification) -> dict:
    return {
        "id": notification.id,
        "type": notification.notification_type,
        "title": notification.title,
        "body": notification.body,
        "payload": notification.payload,
        "scheduled_for": notification.scheduled_for.isoformat() if notification.scheduled_for else None,
        "created_at": notification.created_at.isoformat(),
        "read_at": notification.read_at.isoformat() if notification.read_at else None,
        "invite_id": notification.invite_id,
    }


def _close_invite_request_notifications(invite: MeetupInvite) -> None:
    MeetupNotification.objects.filter(
        invite=invite,
        notification_type=MeetupNotification.Type.INVITE_REQUEST,
        read_at__isnull=True,
    ).update(read_at=timezone.now())


def _create_meetup_notification(
    *,
    recipient,
    notification_type: str,
    title: str,
    body: str,
    invite: MeetupInvite | None = None,
    scheduled_for=None,
    payload: dict | None = None,
) -> MeetupNotification:
    return MeetupNotification.objects.create(
        recipient_profile=recipient,
        invite=invite,
        notification_type=notification_type,
        title=title,
        body=body,
        scheduled_for=scheduled_for,
        payload=payload or {},
    )


def _next_accepted_meeting(profile, exclude_invite_id: int | None = None) -> MeetupInvite | None:
    qs = MeetupInvite.objects.select_related("requester_profile", "invited_profile").filter(
        Q(requester_profile=profile) | Q(invited_profile=profile),
        status=MeetupInvite.Status.ACCEPTED,
        proposed_time__gte=timezone.now(),
    )
    if exclude_invite_id is not None:
        qs = qs.exclude(pk=exclude_invite_id)
    return qs.order_by("proposed_time", "id").first()


def _normalize_friend_name(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _accepted_friends_for_profile(profile):
    requests = FriendRequest.objects.select_related(
        "from_profile__user",
        "from_profile__elder_profile",
        "to_profile__user",
        "to_profile__elder_profile",
    ).filter(
        status=FriendRequest.Status.ACCEPTED,
    ).filter(
        Q(from_profile=profile) | Q(to_profile=profile),
    )
    return [
        request_obj.to_profile if request_obj.from_profile_id == profile.id else request_obj.from_profile
        for request_obj in requests
    ]


def _resolve_meetup_friend(viewer, body: dict):
    friend_user_id_raw = body.get("friend_user_id")
    friend_name_raw = str(body.get("friend_name") or "").strip()
    has_friend_user_id = friend_user_id_raw not in {None, ""}
    has_friend_name = bool(friend_name_raw)

    if has_friend_user_id and has_friend_name:
        return _json_error(
            "Provide exactly one friend selector. Use friend_name or friend_user_id, not both.",
            code="MULTIPLE_FRIEND_SELECTORS",
        )

    resolved_by_id = None
    if has_friend_user_id:
        try:
            friend_user_id = int(friend_user_id_raw)
        except (TypeError, ValueError):
            return _json_error("friend_user_id must be a valid integer.", code="INVALID_FRIEND")

        resolved_by_id = get_object_or_404(
            type(viewer).objects.select_related("user", "elder_profile"),
            user_id=friend_user_id,
        )
        if resolved_by_id.id == viewer.id:
            return _json_error("You cannot create a meetup with yourself.", code="INVALID_PARTICIPANT")
        if get_friendship_status(viewer, resolved_by_id) != FriendRequest.Status.ACCEPTED:
            return _json_error(
                "Meetups can be proposed only to accepted friends.",
                status_code=403,
                code="FRIEND_REQUIRED",
            )

    if has_friend_name:
        normalized_name = _normalize_friend_name(friend_name_raw)
        friends = _accepted_friends_for_profile(viewer)
        matches = [
            friend for friend in friends
            if _normalize_friend_name(friend.display_name) == normalized_name
        ]
        if not matches:
            return _json_error(
                "No accepted friend was found with that name.",
                status_code=404,
                code="FRIEND_NOT_FOUND",
            )
        if len(matches) > 1:
            return _json_error(
                "More than one accepted friend has that name. Enter the full unique display name.",
                code="FRIEND_NAME_AMBIGUOUS",
            )
        resolved_by_name = matches[0]
        if resolved_by_id is not None and resolved_by_name.id != resolved_by_id.id:
            return _json_error(
                "friend_user_id and friend_name refer to different friends.",
                code="FRIEND_MISMATCH",
            )
        return resolved_by_name

    if resolved_by_id is not None:
        return resolved_by_id

    return _json_error(
        "Provide friend_name or friend_user_id for an accepted friend.",
        code="MISSING_FRIEND",
    )

class RecommendMeetupView(APIView):
    def post(self, request):
        participants = request.data.get('participants', [])
        if not participants:
            return Response(
                {
                    'status': 'error',
                    'message': 'Invalid request payload.',
                    'error': 'Please provide at least one participant coordinate.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        participant_descriptions = request.data.get('participant_descriptions')
        if participant_descriptions is not None and not isinstance(participant_descriptions, list):
            return Response(
                {
                    'status': 'error',
                    'message': 'Invalid request payload.',
                    'error': 'participant_descriptions must be a list of strings when provided.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        participant_vectors = request.data.get('participant_vectors')
        if participant_vectors is not None and not isinstance(participant_vectors, list):
            return Response(
                {
                    'status': 'error',
                    'message': 'Invalid request payload.',
                    'error': 'participant_vectors must be a list of objects when provided.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        preferred_time = None
        preferred_time_raw = str(request.data.get('preferred_time') or '').strip()
        if preferred_time_raw:
            try:
                preferred_time = datetime.fromisoformat(preferred_time_raw)
                if preferred_time.tzinfo is None:
                    preferred_time = timezone.make_aware(preferred_time, timezone.get_current_timezone())
            except ValueError:
                return Response(
                    {
                        'status': 'error',
                        'message': 'Invalid request payload.',
                        'error': 'preferred_time must be an ISO-8601 datetime string.',
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        
        top_n = int(request.data.get('top_n') or 5)
        top_n = max(1, min(top_n, 10))

        recommendations = get_ranked_meetup_spots(
            participants,
            participant_vectors=participant_vectors,
            participant_descriptions=participant_descriptions,
            preferred_time=preferred_time,
            top_n=top_n,
        )
        best_match = recommendations[0] if recommendations else None
        center = get_central_point(participants)
        
        if not best_match:
            return Response(
                {
                    'status': 'error',
                    'message': 'No suitable meetup spot found.',
                    'error': 'Could not find a suitable meeting spot. Ensure API keys are correct and there are places nearby.',
                },
                status=status.HTTP_404_NOT_FOUND,
            )
            
        return Response({
            'status': 'success',
            'message': 'Meetup recommendation generated.',
            'best_match': best_match,
            'recommendations': recommendations,
            'center': center,
            'participants': participants
        })


@csrf_exempt
@require_http_methods(["POST"])
def propose_friend_meetup(request):
    viewer = _require_profile(request)
    if isinstance(viewer, JsonResponse):
        return viewer

    try:
        body = _parse_body(request)
    except Exception:
        return _json_error("Invalid JSON body.")

    friend_profile = _resolve_meetup_friend(viewer, body)
    if isinstance(friend_profile, JsonResponse):
        return friend_profile

    viewer_meeting = _next_accepted_meeting(viewer)
    if viewer_meeting is not None:
        return _json_error(
            "You already have an accepted upcoming meetup and cannot create another one.",
            status_code=409,
            code="MEETING_ALREADY_SCHEDULED",
        )

    friend_meeting = _next_accepted_meeting(friend_profile)
    if friend_meeting is not None:
        return _json_error(
            "Your friend already has an accepted upcoming meetup.",
            status_code=409,
            code="FRIEND_ALREADY_SCHEDULED",
        )

    requester_location = body.get("requester_location") or {}
    friend_location = body.get("friend_location") or {}

    req_lat = requester_location.get("lat", viewer.home_lat)
    req_lng = requester_location.get("lng", viewer.home_lng)
    fr_lat = friend_location.get("lat", friend_profile.home_lat)
    fr_lng = friend_location.get("lng", friend_profile.home_lng)
    if None in {req_lat, req_lng, fr_lat, fr_lng}:
        return _json_error(
            "Both users need location coordinates. Set home_lat/home_lng in profile or send requester_location/friend_location.",
            code="LOCATION_REQUIRED",
        )

    preferred_time_raw = str(body.get("proposed_time") or "").strip()
    preferred_time = None
    if preferred_time_raw:
        try:
            preferred_time = datetime.fromisoformat(preferred_time_raw)
            if preferred_time.tzinfo is None:
                preferred_time = timezone.make_aware(preferred_time, timezone.get_current_timezone())
        except ValueError:
            return _json_error("Invalid proposed_time. Use ISO-8601 format.", code="INVALID_TIME")

    participants = [
        {"lat": float(req_lat), "lng": float(req_lng)},
        {"lat": float(fr_lat), "lng": float(fr_lng)},
    ]
    participant_vectors = [
        (viewer.elder_profile.feature_vector if viewer.elder_profile_id else {}) or {},
        (friend_profile.elder_profile.feature_vector if friend_profile.elder_profile_id else {}) or {},
    ]
    participant_descriptions = [
        viewer.effective_description or viewer.description or '',
        friend_profile.effective_description or friend_profile.description or '',
    ]

    best_match = get_best_meetup_spot(
        participants,
        participant_vectors=participant_vectors,
        participant_descriptions=participant_descriptions,
        preferred_time=preferred_time,
    )
    center = get_central_point(participants)
    if not best_match or not center:
        return _json_error(
            "Could not find a suitable meeting spot for these locations and preferences.",
            status_code=404,
            code="MEETUP_NOT_FOUND",
        )

    proposed_time = preferred_time
    if proposed_time is None:
        proposed_time = datetime.strptime(best_match["recommended_time"], "%Y-%m-%d %H:00")
        proposed_time = timezone.make_aware(proposed_time, timezone.get_current_timezone())

    invite = MeetupInvite.objects.create(
        requester_profile=viewer,
        invited_profile=friend_profile,
        status=MeetupInvite.Status.PENDING,
        proposed_time=proposed_time,
        place_name=best_match.get("place_name") or "Suggested spot",
        place_lat=float(best_match.get("place_lat") or center["lat"]),
        place_lng=float(best_match.get("place_lng") or center["lng"]),
        center_lat=float(center["lat"]),
        center_lng=float(center["lng"]),
        weather=best_match.get("weather") or "",
        temperature=best_match.get("temperature"),
        score=float(best_match.get("score") or 0.0),
        payload={
            "participants": participants,
            "best_match": best_match,
            "notification_message": (
                f"{viewer.display_name} предлага среща в {best_match.get('place_name')} в "
                f"{proposed_time.strftime('%H:%M')} ч. Приемаш ли?"
            ),
        },
    )

    # Treat proposal as an activity signal for the social graph without changing forecast pipeline.
    record_recommendation_activity(
        viewer,
        event_type=RecommendationActivity.EventType.RECOMMENDATION_CLICKED,
        target=friend_profile,
        discovery_mode=RecommendationActivity.DiscoveryMode.DIRECT,
        metadata={"surface": "meetup_invite", "invite_id": invite.id},
    )

    local_time = timezone.localtime(proposed_time)
    invite_notification = _create_meetup_notification(
        recipient=friend_profile,
        notification_type=MeetupNotification.Type.INVITE_REQUEST,
        title="Нова покана за среща",
        body=(
            f"{viewer.display_name} предлага среща в {invite.place_name} на "
            f"{local_time.strftime('%d.%m.%Y')} в {local_time.strftime('%H:%M')} ч. Приемаш ли?"
        ),
        invite=invite,
        payload={
            "requires_response": True,
            "actions": ["accept", "decline"],
            "meeting_place": invite.place_name,
            "meeting_time": invite.proposed_time.isoformat(),
        },
    )

    return _json_ok(
        {
            "status": "success",
            "message": "Meetup proposal created and notification queued for friend confirmation.",
            "invite": _invite_payload(invite, viewer.id),
            "notification": _notification_payload(invite_notification),
        },
        status_code=201,
    )


@require_http_methods(["GET"])
def meetup_invites_collection(request):
    viewer = _require_profile(request)
    if isinstance(viewer, JsonResponse):
        return viewer

    incoming_qs = MeetupInvite.objects.select_related("requester_profile", "invited_profile").filter(invited_profile=viewer)
    outgoing_qs = MeetupInvite.objects.select_related("requester_profile", "invited_profile").filter(requester_profile=viewer)
    return _json_ok(
        {
            "incoming": [_invite_payload(item, viewer.id) for item in incoming_qs],
            "outgoing": [_invite_payload(item, viewer.id) for item in outgoing_qs],
        }
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def meetup_notifications_collection(request):
    viewer = _require_profile(request)
    if isinstance(viewer, JsonResponse):
        return viewer

    if request.method == "POST":
        try:
            body = _parse_body(request)
        except Exception:
            return _json_error("Invalid JSON body.")

        mark_ids = body.get("notification_ids") or []
        if not isinstance(mark_ids, list):
            return _json_error("notification_ids must be a list.", code="INVALID_NOTIFICATION_IDS")
        now = timezone.now()
        updated = MeetupNotification.objects.filter(
            recipient_profile=viewer,
            id__in=mark_ids,
            read_at__isnull=True,
        ).update(read_at=now)
        return _json_ok({"status": "success", "updated": updated})

    notifications = MeetupNotification.objects.filter(recipient_profile=viewer)[:100]
    due_reminders = [
        item
        for item in notifications
        if item.notification_type == MeetupNotification.Type.REMINDER_20M
        and item.read_at is None
        and item.scheduled_for is not None
        and item.scheduled_for <= timezone.now()
    ]
    visible_notifications = [
        item
        for item in notifications
        if item.notification_type != MeetupNotification.Type.REMINDER_20M
        or item.scheduled_for is None
        or item.scheduled_for <= timezone.now()
    ]

    return _json_ok(
        {
            "notifications": [_notification_payload(item) for item in visible_notifications],
            "due_reminders": [_notification_payload(item) for item in due_reminders],
        }
    )


@require_http_methods(["GET"])
def meetup_next_meeting(request):
    viewer = _require_profile(request)
    if isinstance(viewer, JsonResponse):
        return viewer

    next_meeting = _next_accepted_meeting(viewer)
    if next_meeting is None:
        return _json_ok({"has_meeting": False, "meeting": None})

    return _json_ok(
        {
            "has_meeting": True,
            "meeting": _invite_payload(next_meeting, viewer.id),
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def respond_meetup_invite(request, invite_id: int):
    viewer = _require_profile(request)
    if isinstance(viewer, JsonResponse):
        return viewer

    invite = get_object_or_404(
        MeetupInvite.objects.select_related("requester_profile", "invited_profile"),
        pk=invite_id,
    )
    if invite.status != MeetupInvite.Status.PENDING:
        return _json_error("This meetup invite is no longer pending.", code="INVITE_CLOSED")

    try:
        body = _parse_body(request)
    except Exception:
        return _json_error("Invalid JSON body.")

    action = str(body.get("action") or "").strip().lower()
    if action not in {"accept", "decline", "cancel"}:
        return _json_error("action must be one of: accept, decline, cancel.", code="INVALID_ACTION")

    if action in {"accept", "decline"} and invite.invited_profile_id != viewer.id:
        return _json_error("Only the invited friend can accept/decline this meetup.", status_code=403)
    if action == "cancel" and invite.requester_profile_id != viewer.id:
        return _json_error("Only the requester can cancel this meetup.", status_code=403)

    if action == "accept":
        viewer_busy = _next_accepted_meeting(viewer, exclude_invite_id=invite.id)
        if viewer_busy is not None:
            return _json_error(
                "You already have an accepted upcoming meetup and cannot accept another one.",
                status_code=409,
                code="MEETING_ALREADY_SCHEDULED",
            )

        requester_busy = _next_accepted_meeting(invite.requester_profile, exclude_invite_id=invite.id)
        if requester_busy is not None:
            return _json_error(
                "Requester already has an accepted upcoming meetup.",
                status_code=409,
                code="REQUESTER_ALREADY_SCHEDULED",
            )

    status_map = {
        "accept": MeetupInvite.Status.ACCEPTED,
        "decline": MeetupInvite.Status.DECLINED,
        "cancel": MeetupInvite.Status.CANCELED,
    }
    invite.status = status_map[action]
    invite.responded_at = timezone.now()
    invite.save(update_fields=["status", "responded_at", "updated_at"])
    _close_invite_request_notifications(invite)

    generated_notifications = []
    if action == "accept":
        accepted_note = _create_meetup_notification(
            recipient=invite.requester_profile,
            notification_type=MeetupNotification.Type.INVITE_ACCEPTED,
            title="Поканата е приета",
            body=(
                f"{invite.invited_profile.display_name} прие поканата за среща в {invite.place_name}."
            ),
            invite=invite,
            payload={"action": "accepted", "invite_id": invite.id},
        )
        generated_notifications.append(_notification_payload(accepted_note))

        reminder_time = invite.proposed_time - timedelta(minutes=20)
        for recipient in [invite.requester_profile, invite.invited_profile]:
            reminder_note = _create_meetup_notification(
                recipient=recipient,
                notification_type=MeetupNotification.Type.REMINDER_20M,
                title="Напомняне за среща след 20 минути",
                body=(
                    f"Срещата в {invite.place_name} започва след 20 минути."
                ),
                invite=invite,
                scheduled_for=reminder_time,
                payload={
                    "invite_id": invite.id,
                    "meeting_time": invite.proposed_time.isoformat(),
                    "meeting_place": invite.place_name,
                },
            )
            generated_notifications.append(_notification_payload(reminder_note))

    elif action == "decline":
        declined_note = _create_meetup_notification(
            recipient=invite.requester_profile,
            notification_type=MeetupNotification.Type.INVITE_DECLINED,
            title="Поканата е отказана",
            body=(
                f"{invite.invited_profile.display_name} отказа поканата. Никой не потвърди срещата."
            ),
            invite=invite,
            payload={"action": "declined", "invite_id": invite.id, "all_declined": True},
        )
        generated_notifications.append(_notification_payload(declined_note))

    elif action == "cancel":
        canceled_note = _create_meetup_notification(
            recipient=invite.invited_profile,
            notification_type=MeetupNotification.Type.INVITE_CANCELED,
            title="Поканата е отменена",
            body=(
                f"{invite.requester_profile.display_name} отмени поканата за среща."
            ),
            invite=invite,
            payload={"action": "canceled", "invite_id": invite.id},
        )
        generated_notifications.append(_notification_payload(canceled_note))

    return _json_ok(
        {
            "status": "success",
            "message": f"Meetup invite {action}ed.",
            "invite": _invite_payload(invite, viewer.id),
            "notifications": generated_notifications,
        }
    )
