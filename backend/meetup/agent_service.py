from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Any

from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import AccountProfile, FriendRequest, RecommendationActivity
from apps.accounts.services import (
    get_friendship_status,
    record_recommendation_activity,
)

from .models import MeetupInvite, MeetupNotification
from .services import get_best_meetup_spot, get_central_point


@dataclass(slots=True)
class MeetupRequestError(Exception):
    message: str
    status_code: int = 400
    code: str | None = None

    def __str__(self) -> str:
        return self.message


def invite_payload(invite: MeetupInvite, viewer_profile_id: int) -> dict[str, Any]:
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


def notification_payload(notification: MeetupNotification) -> dict[str, Any]:
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


def close_invite_request_notifications(invite: MeetupInvite) -> None:
    MeetupNotification.objects.filter(
        invite=invite,
        notification_type=MeetupNotification.Type.INVITE_REQUEST,
        read_at__isnull=True,
    ).update(read_at=timezone.now())


def create_meetup_notification(
    *,
    recipient: AccountProfile,
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


def next_accepted_meeting(profile: AccountProfile, exclude_invite_id: int | None = None) -> MeetupInvite | None:
    qs = MeetupInvite.objects.select_related("requester_profile", "invited_profile").filter(
        Q(requester_profile=profile) | Q(invited_profile=profile),
        status=MeetupInvite.Status.ACCEPTED,
        proposed_time__gte=timezone.now(),
    )
    if exclude_invite_id is not None:
        qs = qs.exclude(pk=exclude_invite_id)
    return qs.order_by("proposed_time", "id").first()


def normalize_friend_name(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def accepted_friends_for_profile(profile: AccountProfile) -> list[AccountProfile]:
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


def resolve_meetup_friend(
    viewer: AccountProfile,
    *,
    friend_user_id: object = None,
    friend_name: object = None,
) -> AccountProfile:
    friend_name_raw = str(friend_name or "").strip()
    has_friend_user_id = friend_user_id not in {None, ""}
    has_friend_name = bool(friend_name_raw)

    if has_friend_user_id and has_friend_name:
        raise MeetupRequestError(
            "Provide exactly one friend selector. Use friend_name or friend_user_id, not both.",
            code="MULTIPLE_FRIEND_SELECTORS",
        )

    resolved_by_id = None
    if has_friend_user_id:
        try:
            normalized_friend_user_id = int(friend_user_id)
        except (TypeError, ValueError) as exc:
            raise MeetupRequestError(
                "friend_user_id must be a valid integer.",
                code="INVALID_FRIEND",
            ) from exc

        resolved_by_id = (
            AccountProfile.objects.select_related("user", "elder_profile")
            .filter(user_id=normalized_friend_user_id)
            .first()
        )
        if resolved_by_id is None:
            raise MeetupRequestError("Accepted friend not found.", status_code=404, code="FRIEND_NOT_FOUND")
        if resolved_by_id.id == viewer.id:
            raise MeetupRequestError("You cannot create a meetup with yourself.", code="INVALID_PARTICIPANT")
        if get_friendship_status(viewer, resolved_by_id) != FriendRequest.Status.ACCEPTED:
            raise MeetupRequestError(
                "Meetups can be proposed only to accepted friends.",
                status_code=403,
                code="FRIEND_REQUIRED",
            )

    if has_friend_name:
        normalized_name = normalize_friend_name(friend_name_raw)
        matches = [
            friend for friend in accepted_friends_for_profile(viewer)
            if normalize_friend_name(friend.display_name) == normalized_name
        ]
        if not matches:
            raise MeetupRequestError(
                "No accepted friend was found with that name.",
                status_code=404,
                code="FRIEND_NOT_FOUND",
            )
        if len(matches) > 1:
            raise MeetupRequestError(
                "More than one accepted friend has that name. Enter the full unique display name.",
                code="FRIEND_NAME_AMBIGUOUS",
            )
        resolved_by_name = matches[0]
        if resolved_by_id is not None and resolved_by_name.id != resolved_by_id.id:
            raise MeetupRequestError(
                "friend_user_id and friend_name refer to different friends.",
                code="FRIEND_MISMATCH",
            )
        return resolved_by_name

    if resolved_by_id is not None:
        return resolved_by_id

    raise MeetupRequestError(
        "Provide friend_name or friend_user_id for an accepted friend.",
        code="MISSING_FRIEND",
    )


def create_friend_meetup_proposal(
    *,
    viewer: AccountProfile,
    friend_user_id: object = None,
    friend_name: object = None,
    requester_location: dict | None = None,
    friend_location: dict | None = None,
    proposed_time: datetime | None = None,
    spot_picker=None,
) -> tuple[MeetupInvite, MeetupNotification]:
    friend_profile = resolve_meetup_friend(
        viewer,
        friend_user_id=friend_user_id,
        friend_name=friend_name,
    )

    viewer_meeting = next_accepted_meeting(viewer)
    if viewer_meeting is not None:
        raise MeetupRequestError(
            "You already have an accepted upcoming meetup and cannot create another one.",
            status_code=409,
            code="MEETING_ALREADY_SCHEDULED",
        )

    friend_meeting = next_accepted_meeting(friend_profile)
    if friend_meeting is not None:
        raise MeetupRequestError(
            "Your friend already has an accepted upcoming meetup.",
            status_code=409,
            code="FRIEND_ALREADY_SCHEDULED",
        )

    requester_location = requester_location or {}
    friend_location = friend_location or {}
    req_lat = requester_location.get("lat", viewer.home_lat)
    req_lng = requester_location.get("lng", viewer.home_lng)
    fr_lat = friend_location.get("lat", friend_profile.home_lat)
    fr_lng = friend_location.get("lng", friend_profile.home_lng)
    if None in {req_lat, req_lng, fr_lat, fr_lng}:
        raise MeetupRequestError(
            "Both users need location coordinates. Set home_lat/home_lng in profile or send requester_location/friend_location.",
            code="LOCATION_REQUIRED",
        )

    participants = [
        {"lat": float(req_lat), "lng": float(req_lng)},
        {"lat": float(fr_lat), "lng": float(fr_lng)},
    ]
    participant_vectors = [
        (viewer.elder_profile.feature_vector if viewer.elder_profile_id else {}) or {},
        (friend_profile.elder_profile.feature_vector if friend_profile.elder_profile_id else {}) or {},
    ]
    participant_descriptions = [
        viewer.effective_description or viewer.description or "",
        friend_profile.effective_description or friend_profile.description or "",
    ]

    best_match = (spot_picker or get_best_meetup_spot)(
        participants,
        participant_vectors=participant_vectors,
        participant_descriptions=participant_descriptions,
        preferred_time=proposed_time,
    )
    center = get_central_point(participants)
    if not best_match or not center:
        raise MeetupRequestError(
            "Could not find a suitable meeting spot for these locations and preferences.",
            status_code=404,
            code="MEETUP_NOT_FOUND",
        )

    actual_time = proposed_time
    if actual_time is None:
        actual_time = datetime.strptime(best_match["recommended_time"], "%Y-%m-%d %H:00")
        actual_time = timezone.make_aware(actual_time, timezone.get_current_timezone())

    invite = MeetupInvite.objects.create(
        requester_profile=viewer,
        invited_profile=friend_profile,
        status=MeetupInvite.Status.PENDING,
        proposed_time=actual_time,
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
                f"{actual_time.strftime('%H:%M')} ч. Приемаш ли?"
            ),
        },
    )

    record_recommendation_activity(
        viewer,
        event_type=RecommendationActivity.EventType.RECOMMENDATION_CLICKED,
        target=friend_profile,
        discovery_mode=RecommendationActivity.DiscoveryMode.DIRECT,
        metadata={"surface": "meetup_invite", "invite_id": invite.id},
    )

    local_time = timezone.localtime(actual_time)
    invite_notification = create_meetup_notification(
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

    return invite, invite_notification


class MeetupAgentService:
    BULGARIAN_MEETUP_PATTERNS = (
        r"\bискам\s+да\s+изл(?:е|я)за\s+с\s+(.+)$",
        r"\bискам\s+да\s+изляза\s+с\s+(.+)$",
    )

    def resolve_profile(self, agent_user_id: str | None) -> AccountProfile:
        clean_user_id = str(agent_user_id or "").strip()
        if not clean_user_id:
            raise ValueError("Authenticated meetup actions require a user id.")
        profile = (
            AccountProfile.objects.select_related("user", "elder_profile")
            .filter(user_id=clean_user_id)
            .first()
        )
        if profile is None:
            raise ValueError("Account profile not found for meetup flow.")
        return profile

    def extract_friend_name(self, prompt: str) -> str:
        clean_prompt = " ".join(str(prompt or "").split()).strip()
        if not clean_prompt:
            return ""
        for pattern in self.BULGARIAN_MEETUP_PATTERNS:
            match = re.search(pattern, clean_prompt, flags=re.IGNORECASE)
            if match:
                return self._clean_friend_name(match.group(1))
        return ""

    def propose_friend_meetup_for_prompt(
        self,
        *,
        agent_user_id: str | None,
        prompt: str,
        friend_name: str | None = None,
    ) -> dict[str, Any]:
        clean_prompt = " ".join(str(prompt or "").split()).strip()
        if not clean_prompt:
            raise ValueError("prompt required")

        viewer = self.resolve_profile(agent_user_id)
        resolved_friend_name = self._clean_friend_name(friend_name) or self.extract_friend_name(clean_prompt)
        if not resolved_friend_name:
            raise ValueError("Friend name is required for meetup planning.")

        proposed_time = timezone.now() + timedelta(hours=2)
        try:
            invite, notification = create_friend_meetup_proposal(
                viewer=viewer,
                friend_name=resolved_friend_name,
                proposed_time=proposed_time,
            )
        except MeetupRequestError as exc:
            raise ValueError(exc.message) from exc

        invite_data = invite_payload(invite, viewer.id)
        notification_data = notification_payload(notification)
        friend_display_name = (
            invite_data["invited_display_name"]
            if invite.requester_profile_id == viewer.id
            else invite_data["requester_display_name"]
        )
        return {
            "ok": True,
            "widget_type": "meetup_invite",
            "message": f"Meetup proposal created with {friend_display_name}.",
            "friend_name": friend_display_name,
            "invite": invite_data,
            "notification": notification_data,
            "board_object": {
                "tags": [
                    "kind:meetup_invite",
                    "source:meetup",
                    "entity:meetup_invite",
                ],
                "extra_data": {
                    "kind": "meetup_invite",
                    "invite_id": invite.id,
                    "friend_name": friend_display_name,
                    "place_name": invite.place_name,
                    "meeting_when_bg": invite_data.get("meeting_when_bg"),
                    "status": invite.status,
                },
            },
        }

    def _clean_friend_name(self, value: object) -> str:
        candidate = " ".join(str(value or "").split()).strip()
        candidate = candidate.strip("\"'`.,!?;:- ")
        return candidate
