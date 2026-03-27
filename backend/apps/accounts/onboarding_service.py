from __future__ import annotations

import re
import secrets
from typing import Any

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from apps.agent_core.llm_client import LLMClient

from .models import AccountProfile, OnboardingDraft, normalize_phone_number
from .services import (
    build_dynamic_profile_summary,
    build_voice_username,
    issue_token,
    seed_social_graph_for_profile,
    split_display_name,
    sync_profile_to_recommendations,
)


_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "display_name": {"type": ["string", "null"]},
        "phone_number": {"type": ["string", "null"]},
        "profile_summary": {"type": ["string", "null"]},
        "assistant_reply": {"type": "string"},
    },
    "required": ["display_name", "phone_number", "profile_summary", "assistant_reply"],
    "additionalProperties": False,
}


class OnboardingService:
    def __init__(self, client: LLMClient | None = None):
        self._client = client

    @property
    def client(self) -> LLMClient:
        if self._client is None:
            self._client = LLMClient.from_reasoning_provider("openai")
        return self._client

    def start(self, session_id: str | None = None) -> dict[str, Any]:
        draft = None
        if session_id:
            draft = OnboardingDraft.objects.filter(session_id=session_id).first()
        if draft is None:
            draft = OnboardingDraft.objects.create(session_id=self._new_session_id())
            assistant_reply = (
                "Здравейте. Аз съм тук, за да Ви помогна да започнем спокойно. "
                "Кажете ми малко за себе си, както Ви е удобно."
            )
            self._append_history(draft, "assistant", assistant_reply)
            draft.save(update_fields=["conversation_history", "updated_at"])
        else:
            assistant_reply = self._resume_reply(draft)

        return self._payload(draft, assistant_reply)

    def process_turn(self, session_id: str, user_message: str) -> dict[str, Any]:
        draft = self._require_draft(session_id)
        clean_message = " ".join(str(user_message or "").split()).strip()
        if not clean_message:
            raise ValueError("message required")

        self._append_history(draft, "user", clean_message)
        extracted = self._extract(draft, clean_message)
        self._merge_draft(draft, extracted)

        existing_profile = self._find_existing_profile(
            normalize_phone_number(draft.phone_number),
        )
        if existing_profile is not None:
            draft.phone_number = existing_profile.phone_number
            draft.normalized_phone_number = existing_profile.normalized_phone_number
            draft.current_mode = OnboardingDraft.Mode.LOGIN_CONFIRMATION
            assistant_reply = (
                f"Чух телефонен номер {draft.phone_number or existing_profile.phone_number}. "
                "Изглежда вече имате профил. Ако номерът е правилен и искате да влезете, "
                "кажете да, когато приложението Ви попита."
            )
        else:
            missing = self._missing_fields(draft)
            if not missing:
                draft.current_mode = OnboardingDraft.Mode.READY_TO_REGISTER
                assistant_reply = (
                    extracted.get("assistant_reply")
                    or "Благодаря Ви. Вече имам достатъчно информация и ще подготвя профила Ви."
                )
            else:
                draft.current_mode = OnboardingDraft.Mode.COLLECTING
                assistant_reply = (
                    extracted.get("assistant_reply")
                    or self._follow_up_for_missing(draft, missing)
                )

        self._append_history(draft, "assistant", assistant_reply)
        draft.save()
        return self._payload(draft, assistant_reply, recognized_phone=draft.phone_number)

    def confirm_login(
        self,
        session_id: str,
        *,
        phone_confirmed: bool,
        login_confirmed: bool,
    ) -> dict[str, Any]:
        draft = self._require_draft(session_id)
        profile = self._find_existing_profile(draft.normalized_phone_number)
        if profile is None:
            draft.current_mode = OnboardingDraft.Mode.COLLECTING
            assistant_reply = (
                "Не открих съществуващ профил за този номер. Нека продължим разговора спокойно."
            )
            self._append_history(draft, "assistant", assistant_reply)
            draft.save()
            return self._payload(draft, assistant_reply)

        if not phone_confirmed:
            draft.phone_number = ""
            draft.normalized_phone_number = ""
            draft.current_mode = OnboardingDraft.Mode.COLLECTING
            assistant_reply = (
                "Разбрах. Моля, кажете телефонния номер отново и ще го проверя внимателно."
            )
            self._append_history(draft, "assistant", assistant_reply)
            draft.save()
            return self._payload(draft, assistant_reply)

        if not login_confirmed:
            draft.phone_number = ""
            draft.normalized_phone_number = ""
            draft.current_mode = OnboardingDraft.Mode.COLLECTING
            assistant_reply = (
                "Добре. Няма да Ви вписвам. Кажете друг телефонен номер или продължете да ми "
                "разказвате за себе си."
            )
            self._append_history(draft, "assistant", assistant_reply)
            draft.save()
            return self._payload(draft, assistant_reply)

        token = issue_token(profile.user)
        draft.current_mode = OnboardingDraft.Mode.COMPLETED
        draft.completed_at = timezone.now()
        assistant_reply = f"Разбрах Ви. Добре дошли отново, {profile.display_name}."
        self._append_history(draft, "assistant", assistant_reply)
        draft.save()
        return self._payload(
            draft,
            assistant_reply,
            profile=self._serialize_profile(profile),
            token=token.key,
        )

    def complete_registration(
        self,
        session_id: str,
        *,
        microphone_permission_granted: bool,
        phone_permission_granted: bool,
    ) -> dict[str, Any]:
        draft = self._require_draft(session_id)
        missing = self._missing_fields(draft)
        if missing:
            raise ValueError("Draft is not ready for registration.")
        if self._find_existing_profile(draft.normalized_phone_number) is not None:
            raise ValueError("This phone number already belongs to an existing account.")

        with transaction.atomic():
            user = User.objects.create_user(
                username=build_voice_username(draft.display_name, draft.phone_number),
                email="",
            )
            first_name, last_name = split_display_name(draft.display_name)
            user.first_name = first_name
            user.last_name = last_name
            user.set_unusable_password()
            user.save(update_fields=["first_name", "last_name", "password"])

            profile = AccountProfile.objects.create(
                user=user,
                display_name=draft.display_name,
                phone_number=draft.phone_number,
                description=draft.dynamic_profile_summary,
                dynamic_profile_summary=build_dynamic_profile_summary(
                    display_name=draft.display_name,
                    description=draft.dynamic_profile_summary,
                    onboarding_answers={},
                ),
                onboarding_answers={},
                onboarding_completed=True,
                voice_navigation_enabled=True,
                microphone_permission_granted=microphone_permission_granted,
                phone_permission_granted=phone_permission_granted,
            )
            sync_profile_to_recommendations(profile, preserve_adaptation=False)
            seed_social_graph_for_profile(profile)
            token = issue_token(user)

        draft.current_mode = OnboardingDraft.Mode.COMPLETED
        draft.completed_at = timezone.now()
        assistant_reply = f"Готово, {profile.display_name}. Профилът Ви е създаден."
        self._append_history(draft, "assistant", assistant_reply)
        draft.save()
        return self._payload(
            draft,
            assistant_reply,
            profile=self._serialize_profile(profile),
            token=token.key,
        )

    def _payload(
        self,
        draft: OnboardingDraft,
        assistant_reply: str,
        *,
        recognized_phone: str | None = None,
        profile: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "assistant_reply": assistant_reply,
            "mode": draft.current_mode,
            "draft": {
                "session_id": draft.session_id,
                "display_name": draft.display_name,
                "phone_number": draft.phone_number,
                "dynamic_profile_summary": draft.dynamic_profile_summary,
            },
            "missing_fields": self._missing_fields(draft),
            "recognized_phone": recognized_phone or draft.phone_number,
        }
        if profile is not None:
            payload["profile"] = profile
        if token:
            payload["token"] = token
        return payload

    def _extract(self, draft: OnboardingDraft, user_message: str) -> dict[str, str]:
        try:
            return self._extract_with_llm(draft, user_message)
        except Exception:
            return self._extract_with_fallback(draft, user_message)

    def _extract_with_llm(
        self,
        draft: OnboardingDraft,
        user_message: str,
    ) -> dict[str, str]:
        history = draft.conversation_history[-6:]
        history_text = "\n".join(
            f"{item.get('role', 'unknown')}: {item.get('text', '')}" for item in history
        )
        system_prompt = """
Ти си български асистент за онбординг на възрастни хора.
Извличай само проверими данни от разговора и говори спокойно и естествено.
Трябва да събереш:
- име
- телефонен номер
- кратко, човешко обобщение на характера, интересите и начина на общуване

Не задавай формални въпроси като форма. Говори като в естествен разговор.
Ако вече има известни полета, не ги искай отново освен ако не трябва уточнение.
Върни само JSON.
"""
        user_prompt = f"""
Текущи известни данни:
- име: {draft.display_name or "(липсва)"}
- телефон: {draft.phone_number or "(липсва)"}
- профил: {draft.dynamic_profile_summary or "(липсва)"}

Скорошна история:
{history_text or "(няма)"}

Ново съобщение от потребителя:
{user_message}

Върни JSON със:
- display_name
- phone_number
- profile_summary
- assistant_reply
"""
        result = self.client.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            json_schema=_JSON_SCHEMA,
        )
        return {
            "display_name": str(result.get("display_name") or "").strip(),
            "phone_number": str(result.get("phone_number") or "").strip(),
            "profile_summary": str(result.get("profile_summary") or "").strip(),
            "assistant_reply": str(result.get("assistant_reply") or "").strip(),
        }

    def _extract_with_fallback(
        self,
        draft: OnboardingDraft,
        user_message: str,
    ) -> dict[str, str]:
        phone_number = self._extract_phone(user_message)
        display_name = self._extract_name(user_message)
        profile_summary = self._extract_profile_summary(user_message)
        assistant_reply = self._follow_up_for_missing(
            draft,
            self._missing_fields_preview(
                display_name or draft.display_name,
                phone_number or draft.phone_number,
                self._merge_profile_text(draft.dynamic_profile_summary, profile_summary),
            ),
        )
        return {
            "display_name": display_name,
            "phone_number": phone_number,
            "profile_summary": profile_summary,
            "assistant_reply": assistant_reply,
        }

    def _extract_phone(self, message: str) -> str:
        match = re.search(r"(\+?\d[\d\s\-]{6,}\d)", message)
        return match.group(1).strip() if match else ""

    def _extract_name(self, message: str) -> str:
        patterns = [
            r"(?:аз съм|казвам се|името ми е)\s+([A-ZА-Я][A-Za-zА-Яа-я\-]+(?:\s+[A-ZА-Я][A-Za-zА-Яа-я\-]+)?)",
            r"(?:мен ме наричат)\s+([A-ZА-Я][A-Za-zА-Яа-я\-]+(?:\s+[A-ZА-Я][A-Za-zА-Яа-я\-]+)?)",
        ]
        for pattern in patterns:
            match = re.search(pattern, message, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return ""

    def _extract_profile_summary(self, message: str) -> str:
        cleaned = self._extract_phone(message)
        profile_text = message.replace(cleaned, " ").strip() if cleaned else message.strip()
        profile_text = re.sub(
            r"\b(?:аз съм|казвам се|името ми е|мен ме наричат)\b",
            " ",
            profile_text,
            flags=re.IGNORECASE,
        )
        profile_text = re.sub(r"\s+", " ", profile_text).strip(" ,.-")
        if len(profile_text) < 8:
            return ""
        return profile_text

    def _merge_draft(self, draft: OnboardingDraft, extracted: dict[str, str]) -> None:
        display_name = extracted.get("display_name", "").strip()
        if display_name and not draft.display_name:
            draft.display_name = display_name

        phone_number = extracted.get("phone_number", "").strip()
        if phone_number:
            draft.phone_number = phone_number
            draft.normalized_phone_number = normalize_phone_number(phone_number)

        profile_summary = extracted.get("profile_summary", "").strip()
        if profile_summary:
            draft.dynamic_profile_summary = self._merge_profile_text(
                draft.dynamic_profile_summary,
                profile_summary,
            )

    def _merge_profile_text(self, existing: str, new_text: str) -> str:
        current = str(existing or "").strip()
        incoming = str(new_text or "").strip()
        if not incoming:
            return current
        if not current:
            return incoming
        if incoming.lower() in current.lower():
            return current
        return f"{current}\n{incoming}"

    def _append_history(self, draft: OnboardingDraft, role: str, text: str) -> None:
        history = list(draft.conversation_history or [])
        history.append(
            {
                "role": role,
                "text": text,
                "timestamp": timezone.now().isoformat(),
            }
        )
        draft.conversation_history = history[-24:]

    def _resume_reply(self, draft: OnboardingDraft) -> str:
        if draft.current_mode == OnboardingDraft.Mode.LOGIN_CONFIRMATION and draft.phone_number:
            return (
                f"Продължаваме оттам, докъдето стигнахме. Чух номер {draft.phone_number}. "
                "Ако е правилен и искате да влезете, кажете да."
            )
        if draft.current_mode == OnboardingDraft.Mode.READY_TO_REGISTER:
            return "Вече имам достатъчно информация и мога да довърша регистрацията Ви."
        return "Продължаваме спокойно. Разкажете ми още малко за себе си."

    def _missing_fields(self, draft: OnboardingDraft) -> list[str]:
        return self._missing_fields_preview(
            draft.display_name,
            draft.phone_number,
            draft.dynamic_profile_summary,
        )

    def _missing_fields_preview(
        self,
        display_name: str,
        phone_number: str,
        dynamic_profile_summary: str,
    ) -> list[str]:
        missing: list[str] = []
        if not str(display_name or "").strip():
            missing.append("display_name")
        if not normalize_phone_number(phone_number):
            missing.append("phone_number")
        if not str(dynamic_profile_summary or "").strip():
            missing.append("dynamic_profile_summary")
        return missing

    def _follow_up_for_missing(self, draft: OnboardingDraft, missing: list[str]) -> str:
        if "display_name" in missing:
            return "Как да Ви наричам?"
        if "phone_number" in missing:
            return (
                "Благодаря Ви. За да довърша профила, кажете и телефонния си номер спокойно."
            )
        if "dynamic_profile_summary" in missing:
            return (
                "Разкажете ми още малко за характера си, какво обичате и как се чувствате най-добре с хората."
            )
        return "Благодаря Ви. Продължавайте, слушам Ви."

    def _find_existing_profile(self, normalized_phone_number: str) -> AccountProfile | None:
        if not normalized_phone_number:
            return None
        return (
            AccountProfile.objects.select_related("user", "elder_profile")
            .filter(normalized_phone_number=normalized_phone_number)
            .first()
        )

    def _serialize_profile(self, profile: AccountProfile) -> dict[str, Any]:
        return {
            "user_id": profile.user_id,
            "display_name": profile.display_name,
            "phone_number": profile.phone_number,
            "dynamic_profile_summary": profile.dynamic_profile_summary,
            "description": profile.effective_description,
        }

    def _require_draft(self, session_id: str) -> OnboardingDraft:
        draft = OnboardingDraft.objects.filter(session_id=session_id).first()
        if draft is None:
            raise ValueError("Onboarding session not found.")
        return draft

    def _new_session_id(self) -> str:
        return secrets.token_hex(16)
