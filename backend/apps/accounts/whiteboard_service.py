from __future__ import annotations

from typing import Any, Iterable

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from engine.whiteboard_memory import WhiteboardMemoryStore

from .models import (
    AccountProfile,
    ConnectionMessage,
    ConnectionThread,
    FriendRequest,
)
from .services import (
    get_friend_request_between,
    get_friendship_status,
    record_recommendation_activity,
    refresh_social_edge_for_friendship,
)


class AccountWhiteboardService:
    OBJECT_WIDTH = 220.0
    OBJECT_HEIGHT = 164.0
    OBJECT_INSET = 12.0

    def __init__(self, *, memory_store: WhiteboardMemoryStore | None = None) -> None:
        self.memory_store = memory_store or WhiteboardMemoryStore()

    def resolve_profile(self, user_id: str | None) -> AccountProfile | None:
        clean = " ".join(str(user_id or "").strip().split())
        if not clean:
            return None
        queryset = AccountProfile.objects.select_related("user", "elder_profile")
        if clean.isdigit():
            return queryset.filter(user_id=int(clean)).first()
        return queryset.filter(user__username__iexact=clean).first()

    def load_board_state_for_profile(self, profile: AccountProfile) -> dict[str, Any]:
        raw = profile.whiteboard_state if isinstance(profile.whiteboard_state, dict) else {}
        return self.memory_store.normalize_board_state(raw)

    def save_board_state_for_profile(
        self,
        profile: AccountProfile,
        board_state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        normalized = self.memory_store.normalize_board_state(board_state)
        profile.whiteboard_state = normalized
        profile.save(update_fields=["whiteboard_state", "updated_at"])
        return normalized

    def save_board_state_for_user(
        self,
        user_id: str | None,
        board_state: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        profile = self.resolve_profile(user_id)
        if profile is None:
            return None
        return self.save_board_state_for_profile(profile, board_state)

    def apply_board_commands_for_user(
        self,
        user_id: str | None,
        commands: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        profile = self.resolve_profile(user_id)
        if profile is None:
            return None
        current = self.load_board_state_for_profile(profile)
        updated = self.memory_store.apply_commands(current, commands)
        return self.save_board_state_for_profile(profile, updated)

    @transaction.atomic
    def ensure_thread(
        self,
        viewer: AccountProfile,
        target: AccountProfile,
    ) -> ConnectionThread:
        if viewer.pk == target.pk:
            raise ValueError("Cannot open a connection thread with yourself.")
        low, high = self._ordered_pair(viewer, target)
        thread, _ = ConnectionThread.objects.select_related(
            "participant_low",
            "participant_low__user",
            "participant_low__elder_profile",
            "participant_high",
            "participant_high__user",
            "participant_high__elder_profile",
            "created_by",
        ).get_or_create(
            participant_low=low,
            participant_high=high,
            defaults={"created_by": viewer},
        )
        return thread

    def get_thread_for_profile(
        self,
        viewer: AccountProfile,
        thread_id: int,
    ) -> ConnectionThread:
        thread = (
            ConnectionThread.objects.select_related(
                "participant_low",
                "participant_low__user",
                "participant_low__elder_profile",
                "participant_high",
                "participant_high__user",
                "participant_high__elder_profile",
                "created_by",
            )
            .prefetch_related("messages__sender_profile__user")
            .filter(
                Q(participant_low=viewer) | Q(participant_high=viewer),
                pk=thread_id,
            )
            .first()
        )
        if thread is None:
            raise ValueError("Connection thread not found.")
        return thread

    def sync_thread_objects(
        self,
        thread: ConnectionThread,
        *,
        exclude_profile_ids: Iterable[int] | None = None,
    ) -> None:
        excluded = set(exclude_profile_ids or [])
        participants = [thread.participant_low, thread.participant_high]
        for profile in participants:
            if profile.id in excluded:
                continue
            counterparty = thread.counterparty_for(profile)
            self._upsert_thread_object(
                owner=profile,
                counterparty=counterparty,
                thread=thread,
            )

    def build_thread_payload(
        self,
        viewer: AccountProfile,
        thread: ConnectionThread,
    ) -> dict[str, Any]:
        counterparty = thread.counterparty_for(viewer)
        friend_status = get_friendship_status(viewer, counterparty)
        request_obj = get_friend_request_between(
            viewer,
            counterparty,
            statuses=[
                FriendRequest.Status.PENDING,
                FriendRequest.Status.ACCEPTED,
            ],
        )
        messages = [
            {
                "id": message.id,
                "text": message.body,
                "created_at": message.created_at.isoformat(),
                "sender_user_id": message.sender_profile.user_id,
                "sender_name": message.sender_profile.display_name,
                "is_me": message.sender_profile_id == viewer.id,
            }
            for message in thread.messages.select_related("sender_profile__user").all()[:120]
        ]
        return {
            "id": thread.id,
            "friend_status": friend_status,
            "can_reject_chat": friend_status != FriendRequest.Status.ACCEPTED,
            "counterparty": {
                "user_id": counterparty.user_id,
                "display_name": counterparty.display_name,
                "description": counterparty.effective_description or counterparty.description,
                "phone_number": counterparty.phone_number,
            },
            "friend_request": self._serialize_friend_request(viewer, request_obj),
            "messages": messages,
        }

    @transaction.atomic
    def send_message(
        self,
        viewer: AccountProfile,
        thread: ConnectionThread,
        *,
        message: str,
    ) -> dict[str, Any]:
        clean_message = " ".join(str(message or "").strip().split())
        if not clean_message:
            raise ValueError("message required.")
        if not thread.includes(viewer):
            raise ValueError("Connection thread not found.")
        ConnectionMessage.objects.create(
            thread=thread,
            sender_profile=viewer,
            body=clean_message,
        )
        ConnectionThread.objects.filter(pk=thread.pk).update(updated_at=timezone.now())
        return self.build_thread_payload(viewer, self.get_thread_for_profile(viewer, thread.id))

    @transaction.atomic
    def handle_friendship_action(
        self,
        viewer: AccountProfile,
        thread: ConnectionThread,
        *,
        action: str,
        message: str = "",
    ) -> dict[str, Any] | None:
        clean_action = str(action or "").strip().lower()
        counterparty = thread.counterparty_for(viewer)
        now = timezone.now()

        if clean_action == "send":
            existing = get_friend_request_between(
                viewer,
                counterparty,
                statuses=[
                    FriendRequest.Status.PENDING,
                    FriendRequest.Status.ACCEPTED,
                ],
            )
            if existing and existing.status == FriendRequest.Status.ACCEPTED:
                return self.build_thread_payload(viewer, thread)
            if existing and existing.status == FriendRequest.Status.PENDING:
                if existing.from_profile_id != viewer.id:
                    raise ValueError("There is already an incoming friend request from this user.")
                existing.message = str(message or "").strip()
                existing.responded_at = None
                existing.save(update_fields=["message", "responded_at", "updated_at"])
            else:
                existing = FriendRequest.objects.create(
                    from_profile=viewer,
                    to_profile=counterparty,
                    status=FriendRequest.Status.PENDING,
                    message=str(message or "").strip(),
                )
            record_recommendation_activity(
                viewer,
                event_type="friend_request_sent",
                target=counterparty,
                discovery_mode="direct",
                metadata={"thread_id": thread.id, "request_id": existing.id},
                signal_strength=0.18,
            )
            self.sync_thread_objects(thread)
            return self.build_thread_payload(viewer, thread)

        request_obj = get_friend_request_between(
            viewer,
            counterparty,
            statuses=[
                FriendRequest.Status.PENDING,
                FriendRequest.Status.ACCEPTED,
            ],
        )

        if clean_action == "accept":
            if request_obj is None or request_obj.status != FriendRequest.Status.PENDING:
                raise ValueError("No pending friend request to accept.")
            if request_obj.to_profile_id != viewer.id:
                raise ValueError("Only the receiving user can accept this friend request.")
            request_obj.status = FriendRequest.Status.ACCEPTED
            request_obj.responded_at = now
            request_obj.save(update_fields=["status", "responded_at", "updated_at"])
            refresh_social_edge_for_friendship(request_obj.from_profile, request_obj.to_profile)
            record_recommendation_activity(
                viewer,
                event_type="friend_request_accepted",
                target=counterparty,
                discovery_mode="direct",
                metadata={"thread_id": thread.id, "request_id": request_obj.id},
                signal_strength=0.34,
            )
            self.sync_thread_objects(thread)
            return self.build_thread_payload(viewer, thread)

        if clean_action == "decline":
            if request_obj is None or request_obj.status != FriendRequest.Status.PENDING:
                raise ValueError("No pending friend request to decline.")
            if request_obj.to_profile_id != viewer.id:
                raise ValueError("Only the receiving user can decline this friend request.")
            request_obj.status = FriendRequest.Status.DECLINED
            request_obj.responded_at = now
            request_obj.save(update_fields=["status", "responded_at", "updated_at"])
            record_recommendation_activity(
                viewer,
                event_type="friend_request_declined",
                target=counterparty,
                discovery_mode="direct",
                metadata={"thread_id": thread.id, "request_id": request_obj.id},
                signal_strength=0.12,
            )
            self.sync_thread_objects(thread)
            return self.build_thread_payload(viewer, thread)

        if clean_action == "cancel":
            if request_obj is None or request_obj.status != FriendRequest.Status.PENDING:
                raise ValueError("No pending friend request to cancel.")
            if request_obj.from_profile_id != viewer.id:
                raise ValueError("Only the sending user can cancel this friend request.")
            request_obj.status = FriendRequest.Status.CANCELED
            request_obj.responded_at = now
            request_obj.save(update_fields=["status", "responded_at", "updated_at"])
            record_recommendation_activity(
                viewer,
                event_type="friend_request_canceled",
                target=counterparty,
                discovery_mode="direct",
                metadata={"thread_id": thread.id, "request_id": request_obj.id},
                signal_strength=0.1,
            )
            self.sync_thread_objects(thread)
            return self.build_thread_payload(viewer, thread)

        if clean_action == "unfriend":
            accepted = FriendRequest.objects.filter(
                Q(from_profile=viewer, to_profile=counterparty)
                | Q(from_profile=counterparty, to_profile=viewer),
                status=FriendRequest.Status.ACCEPTED,
            )
            if not accepted.exists():
                raise ValueError("This user is not currently your friend.")
            accepted.update(status=FriendRequest.Status.CANCELED, responded_at=now, updated_at=now)
            self.delete_thread(thread)
            return None

        raise ValueError("Unsupported friendship action.")

    @transaction.atomic
    def reject_thread(
        self,
        viewer: AccountProfile,
        thread: ConnectionThread,
    ) -> None:
        counterparty = thread.counterparty_for(viewer)
        if get_friendship_status(viewer, counterparty) == FriendRequest.Status.ACCEPTED:
            raise ValueError("Friends must be unfriended before removing the chat.")
        pending = get_friend_request_between(
            viewer,
            counterparty,
            statuses=[FriendRequest.Status.PENDING],
        )
        if pending is not None:
            pending.status = (
                FriendRequest.Status.CANCELED
                if pending.from_profile_id == viewer.id
                else FriendRequest.Status.DECLINED
            )
            pending.responded_at = timezone.now()
            pending.save(update_fields=["status", "responded_at", "updated_at"])
        self.delete_thread(thread)

    @transaction.atomic
    def delete_thread(self, thread: ConnectionThread) -> None:
        participants = [thread.participant_low, thread.participant_high]
        for profile in participants:
            self._remove_thread_object(profile, thread)
        thread.delete()

    def _upsert_thread_object(
        self,
        *,
        owner: AccountProfile,
        counterparty: AccountProfile,
        thread: ConnectionThread,
    ) -> None:
        board_state = self.load_board_state_for_profile(owner)
        object_name = self.thread_object_name(thread)
        objects = [dict(item) for item in board_state.get("objects", []) if isinstance(item, dict)]
        existing_index = next(
            (index for index, item in enumerate(objects) if str(item.get("name") or "").strip() == object_name),
            None,
        )
        friend_status = get_friendship_status(owner, counterparty)
        color = self._object_color(friend_status)
        payload = {
            "name": object_name,
            "text": counterparty.display_name,
            "width": self.OBJECT_WIDTH,
            "height": self.OBJECT_HEIGHT,
            "baseScale": 1.0,
            "innerInset": self.OBJECT_INSET,
            "memoryType": "memory",
            "deleteAfterClick": False,
            "color": color,
            "tags": [
                "type:user",
                "entity:user",
                "source:connections",
                f"thread:{thread.id}",
                f"friend_status:{friend_status}",
            ],
            "extraData": {
                "kind": "user_chat",
                "thread_id": thread.id,
                "user_id": counterparty.user_id,
                "display_name": counterparty.display_name,
                "description": counterparty.effective_description or counterparty.description,
                "phone_number": counterparty.phone_number,
                "friend_status": friend_status,
                "is_friend": friend_status == FriendRequest.Status.ACCEPTED,
            },
        }
        if existing_index is not None:
            existing = objects[existing_index]
            payload.update(
                {
                    "x": existing.get("x"),
                    "y": existing.get("y"),
                    "width": existing.get("width", self.OBJECT_WIDTH),
                    "height": existing.get("height", self.OBJECT_HEIGHT),
                    "baseScale": existing.get("baseScale", 1.0),
                    "innerInset": existing.get("innerInset", self.OBJECT_INSET),
                    "color": existing.get("color", color),
                }
            )
            objects[existing_index] = payload
        else:
            empty = self.memory_store.find_largest_empty_space(board_state)
            bbox = empty.get("bbox") if isinstance(empty.get("bbox"), dict) else {}
            x = self._to_float(bbox.get("x"), 28.0)
            y = self._to_float(bbox.get("y"), 28.0)
            width = self._to_float(bbox.get("width"), 0.0)
            height = self._to_float(bbox.get("height"), 0.0)
            if width >= self.OBJECT_WIDTH:
                x += max(0.0, (width - self.OBJECT_WIDTH) / 2.0)
            if height >= self.OBJECT_HEIGHT:
                y += max(0.0, (height - self.OBJECT_HEIGHT) / 2.0)
            payload["x"] = x
            payload["y"] = y
            objects.append(payload)

        self.save_board_state_for_profile(
            owner,
            {
                "board": board_state.get("board", {}),
                "objects": objects,
            },
        )

    def _remove_thread_object(self, owner: AccountProfile, thread: ConnectionThread) -> None:
        board_state = self.load_board_state_for_profile(owner)
        object_name = self.thread_object_name(thread)
        objects = [
            item
            for item in board_state.get("objects", [])
            if str((item or {}).get("name") or "").strip() != object_name
        ]
        self.save_board_state_for_profile(
            owner,
            {
                "board": board_state.get("board", {}),
                "objects": objects,
            },
        )

    def _serialize_friend_request(
        self,
        viewer: AccountProfile,
        request_obj: FriendRequest | None,
    ) -> dict[str, Any] | None:
        if request_obj is None:
            return None
        direction = "outgoing" if request_obj.from_profile_id == viewer.id else "incoming"
        return {
            "id": request_obj.id,
            "status": request_obj.status,
            "direction": direction,
            "message": request_obj.message,
        }

    def _ordered_pair(
        self,
        left: AccountProfile,
        right: AccountProfile,
    ) -> tuple[AccountProfile, AccountProfile]:
        return (left, right) if left.pk < right.pk else (right, left)

    def thread_object_name(self, thread: ConnectionThread) -> str:
        return f"connection_thread_{thread.id}"

    def _object_color(self, friend_status: str) -> int:
        if friend_status == FriendRequest.Status.ACCEPTED:
            return 0xFF6EA886
        if "pending" in friend_status:
            return 0xFFD59667
        return 0xFF6E9ACC

    def _to_float(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)
