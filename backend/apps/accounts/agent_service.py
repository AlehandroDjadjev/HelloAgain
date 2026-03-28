from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Dict, List

from django.contrib.auth.models import User
from django.db import transaction

from engine.llm_parser import QwenPromptParser
from engine.qwen_worker_client import QwenWorkerClient
from engine.user_context import ActiveUserTracker, normalize_phone_number

from .models import AccountProfile
from .services import (
    build_match_summary,
    build_top_traits,
    can_view_email,
    can_view_phone,
    ensure_account_profile,
    get_friendship_status,
    graph_scores_for_profile,
    matched_profile_ids_for_owner,
    recommend_profiles_for_description,
    record_recommendation_activity,
    sync_profile_to_recommendations,
)
from .whiteboard_service import AccountWhiteboardService


def _pretty_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, indent=2)


class ConnectionsAgentService:
    SUPPORTED_PROFILE_FIELDS = {
        "display_name",
        "description",
        "dynamic_profile_summary",
        "profile_notes",
    }

    def __init__(
        self,
        *,
        qwen_client: QwenWorkerClient | None = None,
        user_tracker: ActiveUserTracker | None = None,
    ) -> None:
        self.qwen_client = qwen_client or QwenWorkerClient()
        self.whiteboard_service = AccountWhiteboardService()
        self.user_tracker = user_tracker or ActiveUserTracker()

    def resolve_profile(self, agent_user_id: str | None) -> AccountProfile:
        user_context = self.user_tracker.resolve(user_id=agent_user_id)
        clean = self._clean_text(user_context.get("resolved_user_id")) or "anonymous"
        normalized_phone = normalize_phone_number(user_context.get("phone_number"))

        if normalized_phone:
            profile = (
                AccountProfile.objects.select_related("user", "elder_profile")
                .filter(normalized_phone_number=normalized_phone)
                .first()
            )
            if profile is not None:
                if not profile.elder_profile_id:
                    sync_profile_to_recommendations(profile, preserve_adaptation=False)
                    profile.refresh_from_db()
                return profile

        if clean.isdigit():
            profile = (
                AccountProfile.objects.select_related("user", "elder_profile")
                .filter(user_id=int(clean))
                .first()
            )
            if profile is not None:
                if not profile.elder_profile_id:
                    sync_profile_to_recommendations(profile, preserve_adaptation=False)
                    profile.refresh_from_db()
                return profile

        profile = (
            AccountProfile.objects.select_related("user", "elder_profile")
            .filter(user__username__iexact=clean)
            .first()
        )
        if profile is not None:
            if not profile.elder_profile_id:
                sync_profile_to_recommendations(profile, preserve_adaptation=False)
                profile.refresh_from_db()
            return profile

        username = self._unique_username(clean)
        user = User.objects.create_user(username=username, email="")
        profile = ensure_account_profile(user)
        if normalized_phone:
            profile.phone_number = normalized_phone
        profile.display_name = self._display_name_from_identifier(clean)
        profile.save(update_fields=["phone_number", "display_name"] if normalized_phone else ["display_name"])
        sync_profile_to_recommendations(profile, preserve_adaptation=False)
        return (
            AccountProfile.objects.select_related("user", "elder_profile")
            .get(pk=profile.pk)
        )

    @transaction.atomic
    def update_profile_from_prompt(
        self,
        *,
        agent_user_id: str | None,
        prompt: str,
        profile_patch: Dict[str, Any] | None = None,
        profile_json: Dict[str, Any] | None = None,
        board_state: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        clean_prompt = self._clean_text(prompt)
        if not clean_prompt:
            raise ValueError("prompt required")

        profile = self.resolve_profile(agent_user_id)
        generated_patch = self._generate_profile_patch(
            prompt=clean_prompt,
            profile=profile,
            profile_json=profile_json,
            board_state=board_state,
            explicit_patch=profile_patch,
        )
        applied_patch = self._apply_profile_patch(profile, generated_patch)
        elder_profile = sync_profile_to_recommendations(profile, preserve_adaptation=True)

        return {
            "ok": True,
            "agent_user_id": self._clean_text(agent_user_id) or str(profile.user_id),
            "reasoning_summary": self._clean_text(generated_patch.get("reasoning_summary")),
            "profile_patch": applied_patch,
            "profile": self._profile_payload(profile),
            "vector_profile": {
                "elder_profile_id": elder_profile.id,
                "feature_vector_version": elder_profile.feature_vector_version,
                "vector_source": elder_profile.vector_source,
            },
            "message": "Profile updated and recommendation vectors refreshed.",
        }

    def find_connection_for_prompt(
        self,
        *,
        agent_user_id: str | None,
        prompt: str,
        limit: int = 1,
        board_state: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        clean_prompt = self._clean_text(prompt)
        if not clean_prompt:
            raise ValueError("prompt required")

        viewer = self.resolve_profile(agent_user_id)
        temporary_description = self._generate_connection_description(
            prompt=clean_prompt,
            profile=viewer,
            board_state=board_state,
        )
        rows = recommend_profiles_for_description(
            viewer,
            description=temporary_description,
            limit=max(1, min(int(limit or 1), 5)),
        )
        record_recommendation_activity(
            viewer,
            event_type="description_query_submitted",
            discovery_mode="describe_someone",
            query_text=temporary_description,
            metadata={"prompt": clean_prompt, "result_count": len(rows)},
            signal_strength=0.06,
        )

        if not rows:
            return {
                "ok": True,
                "query_description": temporary_description,
                "user": None,
                "board_object": {
                    "tags": ["source:connections"],
                    "extra_data": {
                        "kind": "user_search",
                        "query_description": temporary_description,
                    },
                },
                "message": "No connection match was found.",
            }

        chosen = rows[0]
        target = (
            AccountProfile.objects.select_related("user", "elder_profile")
            .filter(user_id=chosen["user_id"])
            .first()
        )
        thread = None
        if target is not None and target.pk != viewer.pk:
            thread = self.whiteboard_service.ensure_thread(viewer, target)
            self.whiteboard_service.sync_thread_objects(
                thread,
                exclude_profile_ids={viewer.id},
            )
        user_payload = {
            "id": chosen["user_id"],
            "user_id": chosen["user_id"],
            "name": chosen["display_name"],
            "display_name": chosen["display_name"],
            "description": chosen["description"],
            "friend_status": chosen.get("friend_status"),
            "match_percent": chosen.get("match_percent"),
            "raw_score": chosen.get("raw_score"),
            "thread_id": thread.id if thread is not None else None,
        }
        return {
            "ok": True,
            "query_description": temporary_description,
            "user": user_payload,
            "recommendation": chosen,
            "board_object": {
                "tags": [
                    "type:user",
                    "entity:user",
                    "source:connections",
                ],
                "extra_data": {
                    "kind": "user",
                    "id": chosen["user_id"],
                    "user_id": chosen["user_id"],
                    "elder_profile_id": chosen.get("elder_profile_id"),
                    "display_name": chosen["display_name"],
                    "description": chosen["description"],
                    "phone_number": target.phone_number if target is not None else "",
                    "thread_id": thread.id if thread is not None else None,
                    "friend_status": chosen.get("friend_status"),
                    "is_friend": chosen.get("friend_status") == "accepted",
                },
            },
            "message": f"Closest connection match is {chosen['display_name']}.",
        }

    def build_user_widget_payload(
        self,
        *,
        agent_user_id: str | None,
        target_user_id: int,
    ) -> Dict[str, Any]:
        viewer = self.resolve_profile(agent_user_id)
        target = (
            AccountProfile.objects.select_related("user", "elder_profile")
            .filter(user_id=int(target_user_id))
            .first()
        )
        if target is None:
            raise ValueError("Target user not found.")

        record_recommendation_activity(
            viewer,
            event_type="search_result_opened",
            target=target,
            discovery_mode="direct",
            metadata={"source": "whitespace_user_object"},
            signal_strength=0.14,
        )
        serialized = self._serialize_profile(target=target, viewer=viewer)
        thread_payload = None
        widget_type = "user_profile"
        if viewer.pk != target.pk:
            thread = self.whiteboard_service.ensure_thread(viewer, target)
            self.whiteboard_service.sync_thread_objects(thread)
            thread_payload = self.whiteboard_service.build_thread_payload(viewer, thread)
            widget_type = "user_connection"
        summary = (
            (serialized.get("match_summary") or {}).get("friendship_summary")
            or serialized.get("description")
            or serialized.get("display_name")
            or "User profile"
        )
        return {
            "widget_type": widget_type,
            "title": serialized["display_name"],
            "summary": self._clean_text(summary),
            "user": serialized,
            "thread": thread_payload,
        }

    def load_board_state_for_user(self, user_id: str | None) -> Dict[str, Any] | None:
        profile = self.whiteboard_service.resolve_profile(user_id)
        if profile is None:
            return None
        return self.whiteboard_service.load_board_state_for_profile(profile)

    def save_board_state_for_user(
        self,
        user_id: str | None,
        board_state: Dict[str, Any] | None,
    ) -> Dict[str, Any] | None:
        return self.whiteboard_service.save_board_state_for_user(user_id, board_state)

    def apply_board_commands_for_user(
        self,
        user_id: str | None,
        commands: List[Dict[str, Any]],
    ) -> Dict[str, Any] | None:
        return self.whiteboard_service.apply_board_commands_for_user(user_id, commands)

    def _generate_profile_patch(
        self,
        *,
        prompt: str,
        profile: AccountProfile,
        profile_json: Dict[str, Any] | None,
        board_state: Dict[str, Any] | None,
        explicit_patch: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        current_profile = self._profile_payload(profile)
        fallback_description = self._fallback_description_update(
            current_profile.get("effective_description", ""),
            prompt,
        )
        fallback_patch = {
            "description": fallback_description,
            "dynamic_profile_summary": current_profile.get("dynamic_profile_summary", ""),
            "profile_notes": current_profile.get("profile_notes", ""),
            "reasoning_summary": "Fallback profile merge from the new prompt.",
        }

        system_prompt = f"""
You update a living social profile for one user.
Use the prompt as NEW REAL information about the user, not as a temporary search query.
Preserve stable facts that already exist and add only what is supported by the prompt.
Do not invent contact info, age, medical facts, or relationships.
Return exactly one JSON object and nothing else.

JSON shape:
{{
  "description": "the merged long-form user description",
  "dynamic_profile_summary": "short active summary",
  "profile_notes": "extra durable notes if useful",
  "reasoning_summary": "short explanation"
}}

Supported profile fields:
{_pretty_json(sorted(self.SUPPORTED_PROFILE_FIELDS))}
""".strip()

        user_prompt = f"""
Update this user profile with the new durable information.

Current profile:
{_pretty_json(current_profile)}

Optional profile json context:
{_pretty_json(profile_json or {})}

Visible whiteboard context:
{_pretty_json(self._board_context(board_state))}

Explicit patch hints:
{_pretty_json(explicit_patch or {})}

New user prompt:
{prompt}
""".strip()

        parsed = self._generate_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            fallback=fallback_patch,
        )
        merged = {
            **fallback_patch,
            **{
                key: parsed.get(key)
                for key in ("description", "dynamic_profile_summary", "profile_notes", "reasoning_summary")
                if key in parsed
            },
        }
        if isinstance(explicit_patch, dict):
            for key in self.SUPPORTED_PROFILE_FIELDS:
                if key in explicit_patch:
                    merged[key] = explicit_patch[key]
        return merged

    def _generate_connection_description(
        self,
        *,
        prompt: str,
        profile: AccountProfile,
        board_state: Dict[str, Any] | None,
    ) -> str:
        current_profile = self._profile_payload(profile)
        profile_description = self._clean_text(
            current_profile.get("effective_description") or current_profile.get("description")
        )
        fallback = " ".join(
            segment for segment in [profile_description, prompt] if self._clean_text(segment)
        ).strip() or prompt
        system_prompt = """
You translate a request into a TEMPORARY search description for finding a helpful human connection.
Build the temporary description from the current user's durable description plus the new prompt.
Do not rewrite this as facts about the current user unless the prompt explicitly says them.
Focus on the type of person, energy, interests, or support style that would fit the moment.
If the whiteboard already holds user objects with extra data, use that as context.
Return exactly one JSON object and nothing else.

JSON shape:
{
  "temporary_description": "2 to 5 sentences describing the best connection to look for",
  "reasoning_summary": "short explanation"
}
""".strip()
        user_prompt = f"""
Current user profile:
{_pretty_json(current_profile)}

Visible whiteboard context:
{_pretty_json(self._board_context(board_state))}

Prompt:
{prompt}
""".strip()
        parsed = self._generate_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            fallback={
                "temporary_description": fallback,
                "reasoning_summary": "Fallback temporary description from the raw prompt.",
            },
        )
        return self._clean_text(parsed.get("temporary_description") or fallback)

    def _apply_profile_patch(
        self,
        profile: AccountProfile,
        patch: Dict[str, Any],
    ) -> Dict[str, str]:
        applied: Dict[str, str] = {}
        changed_fields: List[str] = []
        for field in self.SUPPORTED_PROFILE_FIELDS:
            if field not in patch:
                continue
            value = str(patch.get(field) or "").strip()
            if field == "display_name":
                value = value[:120]
            if getattr(profile, field) == value:
                continue
            setattr(profile, field, value)
            applied[field] = value
            changed_fields.append(field)
        if changed_fields:
            profile.save(update_fields=changed_fields)
        return applied

    def _profile_payload(self, profile: AccountProfile) -> Dict[str, Any]:
        return {
            "user_id": profile.user_id,
            "username": profile.user.username,
            "display_name": profile.display_name,
            "description": profile.description,
            "dynamic_profile_summary": profile.dynamic_profile_summary,
            "profile_notes": profile.profile_notes,
            "effective_description": profile.effective_description,
        }

    def _serialize_profile(
        self,
        *,
        target: AccountProfile,
        viewer: AccountProfile | None,
    ) -> Dict[str, Any]:
        matched_contact_ids = matched_profile_ids_for_owner(viewer) if viewer else set()
        graph_scores = graph_scores_for_profile(viewer) if viewer else {}
        graph_score = (
            float(graph_scores.get(target.elder_profile_id or -1, 0.3))
            if viewer and viewer.pk != target.pk
            else None
        )
        match_summary = build_match_summary(viewer, target, graph_score=graph_score or 0.3)
        return {
            "user_id": target.user_id,
            "elder_profile_id": target.elder_profile_id,
            "username": target.user.username,
            "display_name": target.display_name,
            "description": target.effective_description or target.description,
            "friend_status": get_friendship_status(viewer, target),
            "top_traits": build_top_traits(target),
            "matched_from_contacts": bool(matched_contact_ids and target.id in matched_contact_ids),
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

    def _board_context(self, board_state: Dict[str, Any] | None) -> List[Dict[str, Any]]:
        if not isinstance(board_state, dict):
            return []
        results: List[Dict[str, Any]] = []
        for raw in board_state.get("objects", [])[:12]:
            if not isinstance(raw, dict):
                continue
            entry = {
                "name": self._clean_text(raw.get("name")),
                "text": self._clean_text(raw.get("text")),
                "tags": [
                    self._clean_text(tag)
                    for tag in raw.get("tags", [])
                    if self._clean_text(tag)
                ],
            }
            extra_data = raw.get("extraData", raw.get("extra_data"))
            if isinstance(extra_data, dict) and extra_data:
                entry["extra_data"] = deepcopy(extra_data)
            if entry["name"] or entry["text"] or entry.get("extra_data"):
                results.append(entry)
        return results

    def _generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        fallback: Dict[str, Any],
    ) -> Dict[str, Any]:
        try:
            raw = self.qwen_client.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                generation_overrides={
                    "max_new_tokens": 256,
                    "json_continuation_budget": 0,
                },
            )
            return QwenPromptParser._extract_json(raw)
        except Exception:
            return deepcopy(fallback)

    def _fallback_description_update(self, current_description: str, prompt: str) -> str:
        current = self._clean_text(current_description)
        addition = self._clean_text(prompt)
        if not current:
            return addition
        if not addition:
            return current
        lowered_current = current.lower()
        if addition.lower() in lowered_current:
            return current
        return f"{current} {addition}".strip()

    def _unique_username(self, seed: str) -> str:
        slug = re.sub(r"[^a-z0-9_]+", "_", seed.lower()).strip("_") or "agent_user"
        slug = slug[:150]
        candidate = slug
        counter = 2
        while User.objects.filter(username__iexact=candidate).exists():
            suffix = f"_{counter}"
            candidate = f"{slug[: max(1, 150 - len(suffix))]}{suffix}"
            counter += 1
        return candidate

    def _display_name_from_identifier(self, raw: str) -> str:
        clean = self._clean_text(raw.replace("_", " ").replace("-", " "))
        if not clean:
            return "Agent User"
        return clean[:120]

    @staticmethod
    def _clean_text(value: Any) -> str:
        return " ".join(str(value or "").strip().split())
