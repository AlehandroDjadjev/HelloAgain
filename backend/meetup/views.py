from datetime import datetime, timedelta

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.services import profile_for_token
from services.google_calendar_service import GoogleCalendarService

from .agent_service import (
    MeetupRequestError,
    close_invite_request_notifications,
    create_friend_meetup_proposal,
    create_meetup_notification,
    invite_payload,
    next_accepted_meeting,
    notification_payload,
)
from .models import MeetupInvite, MeetupNotification
from .services import get_best_meetup_spot, get_central_point, get_ranked_meetup_spots


calendar_service = GoogleCalendarService()


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

    preferred_time_raw = str(body.get("proposed_time") or "").strip()
    preferred_time = None
    if preferred_time_raw:
        try:
            preferred_time = datetime.fromisoformat(preferred_time_raw)
            if preferred_time.tzinfo is None:
                preferred_time = timezone.make_aware(preferred_time, timezone.get_current_timezone())
        except ValueError:
            return _json_error("Invalid proposed_time. Use ISO-8601 format.", code="INVALID_TIME")

    try:
        invite, invite_notification = create_friend_meetup_proposal(
            viewer=viewer,
            friend_user_id=body.get("friend_user_id"),
            friend_name=body.get("friend_name"),
            requester_location=body.get("requester_location") or {},
            friend_location=body.get("friend_location") or {},
            proposed_time=preferred_time,
            spot_picker=get_best_meetup_spot,
        )
    except MeetupRequestError as exc:
        return _json_error(exc.message, status_code=exc.status_code, code=exc.code)

    return _json_ok(
        {
            "status": "success",
            "message": "Meetup proposal created and notification queued for friend confirmation.",
            "invite": invite_payload(invite, viewer.id),
            "notification": notification_payload(invite_notification),
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
            "incoming": [invite_payload(item, viewer.id) for item in incoming_qs],
            "outgoing": [invite_payload(item, viewer.id) for item in outgoing_qs],
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
            "notifications": [notification_payload(item) for item in visible_notifications],
            "due_reminders": [notification_payload(item) for item in due_reminders],
        }
    )


@require_http_methods(["GET"])
def meetup_next_meeting(request):
    viewer = _require_profile(request)
    if isinstance(viewer, JsonResponse):
        return viewer

    next_meeting = next_accepted_meeting(viewer)
    if next_meeting is None:
        return _json_ok({"has_meeting": False, "meeting": None})

    return _json_ok(
        {
            "has_meeting": True,
            "meeting": invite_payload(next_meeting, viewer.id),
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
        viewer_busy = next_accepted_meeting(viewer, exclude_invite_id=invite.id)
        if viewer_busy is not None:
            return _json_error(
                "You already have an accepted upcoming meetup and cannot accept another one.",
                status_code=409,
                code="MEETING_ALREADY_SCHEDULED",
            )

        requester_busy = next_accepted_meeting(invite.requester_profile, exclude_invite_id=invite.id)
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
    close_invite_request_notifications(invite)

    generated_notifications = []
    calendar_results = []
    viewer_calendar_result = None
    calendar_speech_text = ""
    if action == "accept":
        accepted_note = create_meetup_notification(
            recipient=invite.requester_profile,
            notification_type=MeetupNotification.Type.INVITE_ACCEPTED,
            title="Поканата е приета",
            body=(
                f"{invite.invited_profile.display_name} прие поканата за среща в {invite.place_name}."
            ),
            invite=invite,
            payload={"action": "accepted", "invite_id": invite.id},
        )
        generated_notifications.append(notification_payload(accepted_note))

        reminder_time = invite.proposed_time - timedelta(minutes=20)
        for recipient in [invite.requester_profile, invite.invited_profile]:
            reminder_note = create_meetup_notification(
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
            generated_notifications.append(notification_payload(reminder_note))

        for participant, friend in (
            (invite.requester_profile, invite.invited_profile),
            (invite.invited_profile, invite.requester_profile),
        ):
            reminder_payload = calendar_service.build_meetup_reminder_payload(
                user_id=str(participant.user_id),
                friend_name=friend.display_name,
                start_dt=invite.proposed_time,
                location=invite.place_name,
                description="Meetup planned through HelloAgain",
                reminder_minutes=30,
            )
            result = calendar_service.create_meetup_reminder(**reminder_payload)
            calendar_results.append(
                {
                    "user_id": str(participant.user_id),
                    "success": bool(result.get("success")),
                    "error": result.get("error"),
                    "event_id": result.get("event_id"),
                    "html_link": result.get("html_link"),
                    "speech_text": (
                        "Добавих срещата в календарът ти."
                        if result.get("success")
                        else "Срещата бе приета,но календарът не работи в момента."
                    ),
                }
            )
        viewer_calendar_result = next(
            (
                item
                for item in calendar_results
                if str(item.get("user_id") or "") == str(viewer.user_id)
            ),
            None,
        )
        if isinstance(viewer_calendar_result, dict):
            calendar_speech_text = str(viewer_calendar_result.get("speech_text") or "").strip()

    elif action == "decline":
        declined_note = create_meetup_notification(
            recipient=invite.requester_profile,
            notification_type=MeetupNotification.Type.INVITE_DECLINED,
            title="Поканата е отказана",
            body=(
                f"{invite.invited_profile.display_name} отказа поканата. Никой не потвърди срещата."
            ),
            invite=invite,
            payload={"action": "declined", "invite_id": invite.id, "all_declined": True},
        )
        generated_notifications.append(notification_payload(declined_note))

    elif action == "cancel":
        canceled_note = create_meetup_notification(
            recipient=invite.invited_profile,
            notification_type=MeetupNotification.Type.INVITE_CANCELED,
            title="Поканата е отменена",
            body=(
                f"{invite.requester_profile.display_name} отмени поканата за среща."
            ),
            invite=invite,
            payload={"action": "canceled", "invite_id": invite.id},
        )
        generated_notifications.append(notification_payload(canceled_note))

    return _json_ok(
        {
            "status": "success",
            "message": f"Meetup invite {action}ed.",
            "invite": invite_payload(invite, viewer.id),
            "notifications": generated_notifications,
            "calendar_results": calendar_results,
            "viewer_calendar_result": viewer_calendar_result,
            "speech_text": calendar_speech_text,
        }
    )
