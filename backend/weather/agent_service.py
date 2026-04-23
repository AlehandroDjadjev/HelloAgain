from __future__ import annotations

from typing import Any

from apps.accounts.models import AccountProfile

from .services import get_current_weather_snapshot


class WeatherAgentService:
    def resolve_profile(self, agent_user_id: str | None) -> AccountProfile | None:
        clean_user_id = str(agent_user_id or "").strip()
        if not clean_user_id:
            return None
        return (
            AccountProfile.objects.select_related("user", "elder_profile")
            .filter(user_id=clean_user_id)
            .first()
        )

    def resolve_location(
        self,
        *,
        agent_user_id: str | None,
        location: dict[str, Any] | None = None,
    ) -> tuple[float, float]:
        candidate = location if isinstance(location, dict) else {}
        try:
            lat = float(candidate.get("lat"))
            lng = float(candidate.get("lng"))
            return lat, lng
        except (TypeError, ValueError):
            pass

        profile = self.resolve_profile(agent_user_id)
        if profile is not None and profile.home_lat is not None and profile.home_lng is not None:
            return float(profile.home_lat), float(profile.home_lng)
        raise ValueError(
            "Location is required for weather. Send location.lat/location.lng or store home coordinates."
        )

    def get_current_weather_for_prompt(
        self,
        *,
        agent_user_id: str | None,
        prompt: str,
        location: dict[str, Any] | None = None,
        timezone_name: str | None = None,
    ) -> dict[str, Any]:
        clean_prompt = " ".join(str(prompt or "").split()).strip()
        if not clean_prompt:
            raise ValueError("prompt required")
        lat, lng = self.resolve_location(agent_user_id=agent_user_id, location=location)
        return get_current_weather_snapshot(
            lat=lat,
            lng=lng,
            timezone_name=timezone_name,
        )

