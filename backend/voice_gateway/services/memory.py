import os

from django.core.cache import caches
from django.core.exceptions import ImproperlyConfigured


class ConversationMemoryService:
    def __init__(self, cache_alias: str = "sessions", max_turns: int | None = None):
        self.cache_alias = cache_alias
        self.max_turns = max_turns or int(
            os.environ.get("VOICE_GATEWAY_HISTORY_TURNS", "6"),
        )
        self._fallback_store: dict[str, list[dict[str, str]]] = {}

    def _cache(self):
        try:
            return caches[self.cache_alias]
        except (ImproperlyConfigured, KeyError, AttributeError):
            return None

    def _cache_key(self, user_id: str, session_id: str) -> str:
        return f"voice_gateway:history:{user_id}:{session_id}"

    def get_history(self, user_id: str, session_id: str) -> list[dict[str, str]]:
        cache_key = self._cache_key(user_id, session_id)
        cache_backend = self._cache()
        if cache_backend is None:
            cached = self._fallback_store.get(cache_key, [])
        else:
            cached = cache_backend.get(cache_key, [])
        if not isinstance(cached, list):
            return []

        history: list[dict[str, str]] = []
        for item in cached:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).strip()
            content = str(item.get("content", "")).strip()
            if role and content:
                history.append({"role": role, "content": content})
        return history

    def append_turn(
        self,
        user_id: str,
        session_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        history = self.get_history(user_id, session_id)
        history.extend(
            [
                {"role": "user", "content": user_text.strip()},
                {"role": "assistant", "content": assistant_text.strip()},
            ],
        )
        keep_messages = max(2, self.max_turns * 2)
        trimmed_history = history[-keep_messages:]
        cache_key = self._cache_key(user_id, session_id)
        cache_backend = self._cache()
        if cache_backend is None:
            self._fallback_store[cache_key] = trimmed_history
            return
        cache_backend.set(cache_key, trimmed_history, timeout=None)

    def clear(self, user_id: str, session_id: str) -> None:
        cache_key = self._cache_key(user_id, session_id)
        cache_backend = self._cache()
        if cache_backend is None:
            self._fallback_store.pop(cache_key, None)
            return
        cache_backend.delete(cache_key)


conversation_memory = ConversationMemoryService()
