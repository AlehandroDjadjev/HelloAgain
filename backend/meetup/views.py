from datetime import datetime

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

from .models import MeetupInvite
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
        "payload": invite.payload,
        "responded_at": invite.responded_at.isoformat() if invite.responded_at else None,
        "created_at": invite.created_at.isoformat(),
        "updated_at": invite.updated_at.isoformat(),
    }

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

    friend_user_id = body.get("friend_user_id")
    if not friend_user_id:
        return _json_error("friend_user_id is required.", code="MISSING_FRIEND")

    friend_profile = get_object_or_404(
        type(viewer).objects.select_related("user", "elder_profile"),
        user_id=friend_user_id,
    )
    if friend_profile.id == viewer.id:
        return _json_error("You cannot create a meetup with yourself.", code="INVALID_PARTICIPANT")

    if get_friendship_status(viewer, friend_profile) != FriendRequest.Status.ACCEPTED:
        return _json_error("Meetups can be proposed only to accepted friends.", status_code=403, code="FRIEND_REQUIRED")

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

    return _json_ok(
        {
            "status": "success",
            "message": "Meetup proposal created and notification queued for friend confirmation.",
            "invite": _invite_payload(invite, viewer.id),
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

    status_map = {
        "accept": MeetupInvite.Status.ACCEPTED,
        "decline": MeetupInvite.Status.DECLINED,
        "cancel": MeetupInvite.Status.CANCELED,
    }
    invite.status = status_map[action]
    invite.responded_at = timezone.now()
    invite.save(update_fields=["status", "responded_at", "updated_at"])

    return _json_ok(
        {
            "status": "success",
            "message": f"Meetup invite {action}ed.",
            "invite": _invite_payload(invite, viewer.id),
        }
    )
