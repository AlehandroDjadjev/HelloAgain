from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def normalize_phone_number(phone_number: str | None) -> str:
    if not phone_number:
        return ""
    trimmed = phone_number.strip()
    if not trimmed:
        return ""
    prefix = "+" if trimmed.startswith("+") else ""
    digits = re.sub(r"\D+", "", trimmed)
    return f"{prefix}{digits}" if digits else ""


def _looks_like_phone_identifier(value: str) -> bool:
    if not value:
        return False
    if re.search(r"[A-Za-z]", value):
        return False
    digits = re.sub(r"\D+", "", value)
    return len(digits) >= 7


def _safe_name(value: str, *, limit: int = 80) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    if slug:
        return slug[:limit]
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
    return f"value_{digest}"


def _bounded_identifier(value: str, *, limit: int = 120) -> str:
    if len(value) <= limit:
        return value
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
    head = value[: max(1, limit - len(digest) - 2)]
    return f"{head}__{digest}"


class ActiveUserTracker:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or Path(__file__).resolve().parent.parent / "runtime" / "semi_agent"
        self.state_path = self.base_dir / "active_user.json"
        self._lock = Lock()

    def resolve(
        self,
        *,
        user_id: str | None = None,
        phone_number: str | None = None,
    ) -> Dict[str, str]:
        explicit_user_id = _clean_text(user_id)
        explicit_phone = normalize_phone_number(phone_number)

        if explicit_phone:
            context = self._build_context(
                resolved_user_id=explicit_phone,
                phone_number=explicit_phone,
                source="phone_number",
            )
            self._store(context)
            return context

        if explicit_user_id and explicit_user_id.lower() != "anonymous":
            normalized_phone = normalize_phone_number(explicit_user_id) if _looks_like_phone_identifier(explicit_user_id) else ""
            context = self._build_context(
                resolved_user_id=normalized_phone or explicit_user_id,
                phone_number=normalized_phone,
                source="user_id_phone" if normalized_phone else "user_id",
            )
            self._store(context)
            return context

        stored = self.load()
        if stored:
            return {
                **stored,
                "source": "active_fallback",
            }

        context = self._build_context(
            resolved_user_id=f"guest_{uuid.uuid4().hex[:12]}",
            phone_number="",
            source="generated_guest",
        )
        self._store(context)
        return context

    def load(self) -> Dict[str, str]:
        with self._lock:
            try:
                payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
        if not isinstance(payload, dict):
            return {}
        resolved_user_id = _clean_text(payload.get("resolved_user_id"))
        if not resolved_user_id or resolved_user_id == "anonymous":
            return {}
        phone_number = normalize_phone_number(payload.get("phone_number"))
        return self._build_context(
            resolved_user_id=resolved_user_id,
            phone_number=phone_number,
            source=_clean_text(payload.get("source")) or "stored",
        )

    def _store(self, context: Dict[str, str]) -> None:
        if context.get("resolved_user_id") in {"", "anonymous"}:
            return
        self.base_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            **context,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self.state_path.write_text(
                json.dumps(payload, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )

    def _build_context(
        self,
        *,
        resolved_user_id: str,
        phone_number: str,
        source: str,
    ) -> Dict[str, str]:
        clean_resolved = _clean_text(resolved_user_id) or "anonymous"
        clean_phone = normalize_phone_number(phone_number)
        record_name = _bounded_identifier(clean_phone or clean_resolved)
        return {
            "resolved_user_id": clean_phone or clean_resolved,
            "phone_number": clean_phone,
            "record_name": record_name,
            "history_key": _safe_name(record_name),
            "source": source,
        }


class TemporaryChatHistoryStore:
    def __init__(self, base_dir: Path | None = None, *, max_messages: int = 5) -> None:
        self.base_dir = base_dir or Path(__file__).resolve().parent.parent / "runtime" / "semi_agent" / "speech_history"
        self.max_messages = max(1, int(max_messages))
        self._lock = Lock()

    def get_messages(self, *, history_key: str, session_id: str) -> List[Dict[str, str]]:
        path = self._history_path(history_key=history_key, session_id=session_id)
        with self._lock:
            try:
                raw_lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []

        messages: List[Dict[str, str]] = []
        for raw_line in raw_lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            role = _clean_text(payload.get("role")).lower()
            content = _clean_text(payload.get("content"))
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        return messages[-self.max_messages :]

    def append_turn(
        self,
        *,
        history_key: str,
        session_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        messages = self.get_messages(history_key=history_key, session_id=session_id)
        clean_user_text = _clean_text(user_text)
        clean_assistant_text = _clean_text(assistant_text)
        if clean_user_text:
            messages.append({"role": "user", "content": clean_user_text})
        if clean_assistant_text:
            messages.append({"role": "assistant", "content": clean_assistant_text})
        trimmed = messages[-self.max_messages :]
        path = self._history_path(history_key=history_key, session_id=session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = "\n".join(json.dumps(item, ensure_ascii=False) for item in trimmed)
        with self._lock:
            path.write_text(raw, encoding="utf-8")

    def _history_path(self, *, history_key: str, session_id: str) -> Path:
        safe_history_key = _safe_name(history_key or "anonymous")
        safe_session_id = _safe_name(_clean_text(session_id) or "default_session")
        return self.base_dir / f"{safe_history_key}__{safe_session_id}.txt"
