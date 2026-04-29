from __future__ import annotations

import logging
from typing import Any

from apps.accounts.models import AccountProfile

from .services import extract_weather_day_offset, get_weather_snapshot


logger = logging.getLogger(__name__)


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
            logger.info("weather location resolved from request lat=%s lng=%s", lat, lng)
            return lat, lng
        except (TypeError, ValueError):
            pass

        profile = self.resolve_profile(agent_user_id)
        if profile is not None and profile.home_lat is not None and profile.home_lng is not None:
            logger.info("weather location resolved from saved home coordinates user_id=%s", clean_user_id)
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
        logger.info("weather snapshot request lat=%s lng=%s timezone=%s", lat, lng, timezone_name)
        payload = get_weather_snapshot(
            lat=lat,
            lng=lng,
            day_offset=extract_weather_day_offset(clean_prompt),
            timezone_name=timezone_name,
        )
        logger.info("weather snapshot success widget_type=%s", payload.get("widget_type"))
        return payload

