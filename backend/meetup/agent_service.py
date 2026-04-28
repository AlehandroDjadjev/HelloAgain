from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
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


_CYRILLIC_TO_LATIN = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sht",
    "ъ": "a",
    "ь": "",
    "ю": "yu",
    "я": "ya",
}


def _compact_name_tokens(value: str) -> str:
    return " ".join(re.findall(r"[0-9a-zа-я]+", str(value or "").casefold()))


def _latinize_bulgarian_name(value: str) -> str:
    compact = _compact_name_tokens(value)
    return "".join(_CYRILLIC_TO_LATIN.get(char, char) for char in compact)


def normalize_friend_name(value: object) -> str:
    return _compact_name_tokens(str(value or ""))


def friend_name_variants(value: object) -> set[str]:
    normalized = normalize_friend_name(value)
    variants = {normalized} if normalized else set()
    latinized = _latinize_bulgarian_name(str(value or ""))
    if latinized:
        variants.add(latinized)
    return variants


def _first_friend_name_tokens(value: object) -> set[str]:
    variants = friend_name_variants(value)
    tokens: set[str] = set()
    for variant in variants:
        first = variant.split(" ", 1)[0].strip()
        if first:
            tokens.add(first)
    return tokens


def _friend_name_similarity(left: object, right: object) -> float:
    left_variants = friend_name_variants(left)
    right_variants = friend_name_variants(right)
    best_score = 0.0
    for left_variant in left_variants:
        for right_variant in right_variants:
            best_score = max(best_score, SequenceMatcher(None, left_variant, right_variant).ratio())
            left_first = left_variant.split(" ", 1)[0].strip()
            right_first = right_variant.split(" ", 1)[0].strip()
            if left_first and right_first:
                best_score = max(best_score, SequenceMatcher(None, left_first, right_first).ratio())
    return best_score


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
        requested_variants = friend_name_variants(friend_name_raw)
        friends = accepted_friends_for_profile(viewer)
        matches = [
            friend
            for friend in friends
            if requested_variants & friend_name_variants(friend.display_name)
        ]
        if not matches and len(requested_variants) == 1:
            requested_first_token = next(iter(requested_variants)).split(" ", 1)[0].strip()
            if requested_first_token:
                matches = [
                    friend
                    for friend in friends
                    if requested_first_token in _first_friend_name_tokens(friend.display_name)
                ]
        if not matches:
            fuzzy_matches: list[tuple[float, AccountProfile]] = []
            for friend in friends:
                score = _friend_name_similarity(friend_name_raw, friend.display_name)
                if score >= 0.82:
                    fuzzy_matches.append((score, friend))
            fuzzy_matches.sort(key=lambda item: item[0], reverse=True)
            if fuzzy_matches:
                best_score = fuzzy_matches[0][0]
                matches = [
                    friend
                    for score, friend in fuzzy_matches
                    if abs(score - best_score) < 0.015
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
    CITY_LOCATIONS = (
        (
            ("sofia", "sofiya", "софия"),
            {
                "label": "Sofia",
                "lat": 42.6977,
                "lng": 23.3219,
                "timezone": "Europe/Sofia",
            },
        ),
        (
            ("plovdiv", "пловдив"),
            {
                "label": "Plovdiv",
                "lat": 42.1354,
                "lng": 24.7453,
                "timezone": "Europe/Sofia",
            },
        ),
        (
            ("varna", "варна"),
            {
                "label": "Varna",
                "lat": 43.2141,
                "lng": 27.9147,
                "timezone": "Europe/Sofia",
            },
        ),
        (
            ("burgas", "бургас"),
            {
                "label": "Burgas",
                "lat": 42.5048,
                "lng": 27.4626,
                "timezone": "Europe/Sofia",
            },
        ),
    )
    BULGARIAN_MEETUP_PATTERNS = (
        r"\bискам\s+да\s+изл(?:е|я)за\s+с\s+(.+)$",
        r"\bискам\s+да\s+изляза\s+с\s+(.+)$",
    )

    def resolve_profile(self, agent_user_id: str | None) -> AccountProfile:
        profile = self.resolve_optional_profile(agent_user_id)
        if profile is None:
            raise ValueError("Account profile not found for meetup flow.")
        return profile

    def resolve_optional_profile(self, agent_user_id: str | None) -> AccountProfile | None:
        clean_user_id = str(agent_user_id or "").strip()
        if not clean_user_id:
            return None
        return (
            AccountProfile.objects.select_related("user", "elder_profile")
            .filter(user_id=clean_user_id)
            .first()
        )

    def extract_friend_name(self, prompt: str) -> str:
        clean_prompt = " ".join(str(prompt or "").split()).strip()
        if not clean_prompt:
            return ""
        for pattern in self.BULGARIAN_MEETUP_PATTERNS:
            match = re.search(pattern, clean_prompt, flags=re.IGNORECASE)
            if match:
                return self._clean_friend_name(match.group(1))
        return ""

    def extract_explicit_city_location(self, prompt: str) -> dict[str, Any] | None:
        clean_prompt = " ".join(str(prompt or "").split()).strip().lower()
        if not clean_prompt:
            return None
        for aliases, payload in self.CITY_LOCATIONS:
            for alias in aliases:
                escaped_alias = re.escape(alias.lower())
                english_match = re.search(
                    rf"(?:^|[\s,])(?:in|near|around|at|within)\s+{escaped_alias}(?:$|[\s,.!?])",
                    clean_prompt,
                    flags=re.IGNORECASE,
                )
                bulgarian_match = re.search(
                    rf"(?:^|[\s,])(?:в|до|около|край|из)\s+{escaped_alias}(?:$|[\s,.!?])",
                    clean_prompt,
                    flags=re.IGNORECASE,
                )
                if english_match or bulgarian_match:
                    return {
                        **payload,
                        "source": "explicit_city",
                    }
        return None

    def _normalize_coordinate_payload(
        self,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        try:
            lat = float(payload.get("lat"))
            lng = float(payload.get("lng"))
        except (TypeError, ValueError):
            return None
        result = {
            "lat": lat,
            "lng": lng,
        }
        timezone_name = str(payload.get("timezone") or "").strip()
        if timezone_name:
            result["timezone"] = timezone_name
        return result

    def resolve_outdoor_location(
        self,
        *,
        prompt: str,
        viewer: AccountProfile | None = None,
        viewer_location: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        explicit_city = self.extract_explicit_city_location(prompt)
        if explicit_city is not None:
            return explicit_city

        normalized_viewer_location = self._normalize_coordinate_payload(viewer_location)
        if normalized_viewer_location is not None:
            return {
                **normalized_viewer_location,
                "label": "your current area",
                "source": "current_location",
            }

        if viewer is not None and viewer.home_lat is not None and viewer.home_lng is not None:
            return {
                "lat": float(viewer.home_lat),
                "lng": float(viewer.home_lng),
                "label": "your saved area",
                "source": "profile_home",
            }

        raise ValueError(
            "Location is required for outdoor place suggestions. Mention a city like Sofia, send current location, or save home coordinates."
        )

    def propose_friend_meetup_for_prompt(
        self,
        *,
        agent_user_id: str | None,
        prompt: str,
        friend_name: str | None = None,
        viewer_location: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_prompt = " ".join(str(prompt or "").split()).strip()
        if not clean_prompt:
            raise ValueError("prompt required")

        viewer = self.resolve_profile(agent_user_id)
        resolved_friend_name = self._clean_friend_name(friend_name) or self.extract_friend_name(clean_prompt)
        if not resolved_friend_name:
            raise ValueError("Friend name is required for meetup planning.")

        try:
            invite, notification = create_friend_meetup_proposal(
                viewer=viewer,
                friend_name=resolved_friend_name,
                requester_location=viewer_location or {},
                proposed_time=None,
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

    def suggest_outing_for_match(
        self,
        *,
        agent_user_id: str | None,
        prompt: str,
        match_user_id: int,
        viewer_location: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_prompt = " ".join(str(prompt or "").split()).strip()
        if not clean_prompt:
            raise ValueError("prompt required")

        viewer = self.resolve_profile(agent_user_id)
        match_profile = (
            AccountProfile.objects.select_related("user", "elder_profile")
            .filter(user_id=int(match_user_id))
            .first()
        )
        if match_profile is None:
            raise ValueError("Matched profile not found for outing suggestion.")

        resolved_location = self.resolve_outdoor_location(
            prompt=clean_prompt,
            viewer=viewer,
            viewer_location=viewer_location,
        )
        req_lat = resolved_location["lat"]
        req_lng = resolved_location["lng"]
        fr_lat = match_profile.home_lat
        fr_lng = match_profile.home_lng
        if None in {req_lat, req_lng, fr_lat, fr_lng}:
            raise ValueError("Location is required for outing suggestions.")

        participants = [
            {"lat": float(req_lat), "lng": float(req_lng)},
            {"lat": float(fr_lat), "lng": float(fr_lng)},
        ]
        participant_vectors = [
            (viewer.elder_profile.feature_vector if viewer.elder_profile_id else {}) or {},
            (match_profile.elder_profile.feature_vector if match_profile.elder_profile_id else {}) or {},
        ]
        participant_descriptions = [
            viewer.effective_description or viewer.description or clean_prompt,
            match_profile.effective_description or match_profile.description or "",
        ]
        best_match = get_best_meetup_spot(
            participants,
            participant_vectors=participant_vectors,
            participant_descriptions=participant_descriptions,
        )
        center = get_central_point(participants)
        if not best_match or not center:
            raise ValueError("Could not find a suitable outing suggestion.")

        friend_status = get_friendship_status(viewer, match_profile)
        return {
            "ok": True,
            "widget_type": "outing_suggestion",
            "message": f"Found a place and time suggestion with {match_profile.display_name}.",
            "user": {
                "user_id": match_profile.user_id,
                "display_name": match_profile.display_name,
                "description": match_profile.effective_description or match_profile.description,
                "friend_status": friend_status,
            },
            "outing": {
                **best_match,
                "center_lat": round(float(center["lat"]), 6),
                "center_lng": round(float(center["lng"]), 6),
                "location_label": str(resolved_location.get("label") or "").strip(),
                "location_source": str(resolved_location.get("source") or "").strip(),
            },
            "board_object": {
                "tags": [
                    "kind:outing_suggestion",
                    "source:meetup",
                    "entity:outing_suggestion",
                ],
                "extra_data": {
                    "kind": "outing_suggestion",
                    "friend_name": match_profile.display_name,
                    "target_user_id": match_profile.user_id,
                    "place_name": best_match.get("place_name"),
                    "recommended_when_bg": best_match.get("recommended_when_bg"),
                },
            },
        }

    def suggest_outdoor_place_for_prompt(
        self,
        *,
        agent_user_id: str | None,
        prompt: str,
        viewer_location: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_prompt = " ".join(str(prompt or "").split()).strip()
        if not clean_prompt:
            raise ValueError("prompt required")

        viewer = self.resolve_optional_profile(agent_user_id)
        resolved_location = self.resolve_outdoor_location(
            prompt=clean_prompt,
            viewer=viewer,
            viewer_location=viewer_location,
        )
        participants = [
            {
                "lat": float(resolved_location["lat"]),
                "lng": float(resolved_location["lng"]),
            }
        ]
        participant_vectors = []
        if viewer is not None and viewer.elder_profile_id:
            participant_vectors.append((viewer.elder_profile.feature_vector or {}).copy())
        participant_descriptions = []
        viewer_description = ""
        if viewer is not None:
            viewer_description = viewer.effective_description or viewer.description or ""
        if viewer_description:
            participant_descriptions.append(viewer_description)
        participant_descriptions.append(clean_prompt)

        best_match = get_best_meetup_spot(
            participants,
            participant_vectors=participant_vectors,
            participant_descriptions=participant_descriptions,
        )
        center = get_central_point(participants)
        if not best_match or not center:
            raise ValueError("Could not find a suitable outdoor place suggestion.")

        location_label = str(resolved_location.get("label") or "").strip()
        title = f"Навън в {location_label}" if location_label else "Предложение за излизане"
        return {
            "ok": True,
            "widget_type": "outing_suggestion",
            "title": title,
            "summary": str(best_match.get("place_name") or "").strip(),
            "message": f"Suggested an outdoor place in {location_label}." if location_label else "Suggested an outdoor place.",
            "user": {},
            "search_location": {
                "label": location_label,
                "source": str(resolved_location.get("source") or "").strip(),
                "lat": round(float(resolved_location["lat"]), 6),
                "lng": round(float(resolved_location["lng"]), 6),
                "timezone": str(resolved_location.get("timezone") or "").strip(),
            },
            "outing": {
                **best_match,
                "center_lat": round(float(center["lat"]), 6),
                "center_lng": round(float(center["lng"]), 6),
                "location_label": location_label,
                "location_source": str(resolved_location.get("source") or "").strip(),
            },
            "board_object": {
                "tags": [
                    "kind:outing_suggestion",
                    "source:meetup",
                    "entity:outing_suggestion",
                ],
                "extra_data": {
                    "kind": "outing_suggestion",
                    "place_name": best_match.get("place_name"),
                    "recommended_when_bg": best_match.get("recommended_when_bg"),
                    "location_label": location_label,
                },
            },
        }

    def _clean_friend_name(self, value: object) -> str:
        candidate = " ".join(str(value or "").split()).strip()
        candidate = candidate.strip("\"'`.,!?;:- ")
        return candidate
