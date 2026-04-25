from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo


class LiveToolError(RuntimeError):
    pass


@dataclass(slots=True)
class ResolvedLocation:
    query: str
    name: str
    latitude: float
    longitude: float
    country: str
    timezone: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "name": self.name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "country": self.country,
            "timezone": self.timezone,
        }


class LocationResolver:
    base_url = "https://geocoding-api.open-meteo.com/v1/search"

    def resolve(self, location: str) -> ResolvedLocation:
        clean_location = " ".join(str(location or "").split()).strip()
        if not clean_location:
            raise LiveToolError("A location is required.")

        payload = self._fetch_json(
            f"{self.base_url}?name={urllib.parse.quote(clean_location)}&count=5&language=bg&format=json"
        )
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results:
            raise LiveToolError(f"Could not resolve location '{clean_location}'.")

        first = self._pick_best_result(clean_location, results)
        name = ", ".join(
            part
            for part in [
                str(first.get("name") or "").strip(),
                str(first.get("admin1") or "").strip(),
                str(first.get("country") or "").strip(),
            ]
            if part
        )
        timezone_name = str(first.get("timezone") or "").strip()
        if not timezone_name:
            raise LiveToolError(f"No timezone data was returned for '{clean_location}'.")

        try:
            latitude = float(first["latitude"])
            longitude = float(first["longitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LiveToolError(f"Incomplete geocoding data for '{clean_location}'.") from exc

        return ResolvedLocation(
            query=clean_location,
            name=name or clean_location,
            latitude=latitude,
            longitude=longitude,
            country=str(first.get("country") or "").strip(),
            timezone=timezone_name,
        )

    def _pick_best_result(self, query: str, results: list[Any]) -> dict[str, Any]:
        clean_query = " ".join(str(query or "").split()).strip().casefold()

        def score(item: Any) -> tuple[int, int, int]:
            if not isinstance(item, dict):
                return (-1, -1, -1)
            name = str(item.get("name") or "").strip().casefold()
            feature_code = str(item.get("feature_code") or "").strip().upper()
            try:
                population = int(item.get("population") or 0)
            except (TypeError, ValueError):
                population = 0
            exact_match = int(name == clean_query)
            admin_score = 2 if feature_code == "PPLC" else 1 if feature_code.startswith("PPL") else 0
            return (exact_match, admin_score, population)

        ranked = sorted(results, key=score, reverse=True)
        top = ranked[0] if ranked and isinstance(ranked[0], dict) else {}
        return top

    def _fetch_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "HelloAgain/1.0 (live-tool-router)"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw_response = response.read()
        except Exception as exc:
            raise LiveToolError(f"Failed to fetch location data: {exc}") from exc
        try:
            return json.loads(raw_response.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise LiveToolError("Location service returned invalid JSON.") from exc


class WeatherService:
    base_url = "https://api.open-meteo.com/v1/forecast"
    weather_codes = {
        0: "clear sky",
        1: "mainly clear",
        2: "partly cloudy",
        3: "overcast",
        45: "fog",
        48: "depositing rime fog",
        51: "light drizzle",
        53: "moderate drizzle",
        55: "dense drizzle",
        56: "light freezing drizzle",
        57: "dense freezing drizzle",
        61: "slight rain",
        63: "moderate rain",
        65: "heavy rain",
        66: "light freezing rain",
        67: "heavy freezing rain",
        71: "slight snow",
        73: "moderate snow",
        75: "heavy snow",
        77: "snow grains",
        80: "slight rain showers",
        81: "moderate rain showers",
        82: "violent rain showers",
        85: "slight snow showers",
        86: "heavy snow showers",
        95: "thunderstorm",
        96: "thunderstorm with slight hail",
        99: "thunderstorm with heavy hail",
    }

    def __init__(self, *, resolver: LocationResolver | None = None) -> None:
        self.resolver = resolver or LocationResolver()

    def get_current_weather(self, location: str) -> dict[str, Any]:
        return self.get_weather(prompt="", location=location)

    def get_weather(
        self,
        *,
        prompt: str,
        location: str | None = None,
        location_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        day_offset = self._extract_day_offset(prompt)
        resolved = self._resolve_weather_location(location=location, location_payload=location_payload)
        params = {
            "latitude": resolved.latitude,
            "longitude": resolved.longitude,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,"
                "apparent_temperature_max,apparent_temperature_min,"
                "precipitation_probability_max,wind_speed_10m_max"
            ),
            "timezone": resolved.timezone,
            "forecast_days": max(day_offset + 1, 2),
        }
        payload = self._fetch_json(f"{self.base_url}?{urllib.parse.urlencode(params)}")
        if day_offset <= 0:
            current = payload.get("current") if isinstance(payload, dict) else None
            if not isinstance(current, dict):
                raise LiveToolError(f"Weather data was unavailable for '{resolved.name}'.")

            weather_code = int(current.get("weather_code") or 0)
            return {
                "tool_name": "weather",
                "forecast_type": "current",
                "location": resolved.as_dict(),
                "temperature_c": current.get("temperature_2m"),
                "apparent_temperature_c": current.get("apparent_temperature"),
                "wind_speed_kmh": current.get("wind_speed_10m"),
                "weather_code": weather_code,
                "weather_description": self.weather_codes.get(weather_code, "unknown conditions"),
                "observed_at": current.get("time"),
                "source": "open-meteo",
            }

        daily = payload.get("daily") if isinstance(payload, dict) else None
        if not isinstance(daily, dict):
            raise LiveToolError(f"Forecast data was unavailable for '{resolved.name}'.")

        try:
            forecast_date = str((daily.get("time") or [])[day_offset])
            weather_code = int((daily.get("weather_code") or [])[day_offset] or 0)
            temperature_max_c = (daily.get("temperature_2m_max") or [])[day_offset]
            temperature_min_c = (daily.get("temperature_2m_min") or [])[day_offset]
            apparent_temperature_max_c = (daily.get("apparent_temperature_max") or [])[day_offset]
            apparent_temperature_min_c = (daily.get("apparent_temperature_min") or [])[day_offset]
            precipitation_probability_max = (daily.get("precipitation_probability_max") or [])[day_offset]
            wind_speed_kmh = (daily.get("wind_speed_10m_max") or [])[day_offset]
        except (IndexError, TypeError, ValueError) as exc:
            raise LiveToolError(f"Forecast data was unavailable for '{resolved.name}'.") from exc

        return {
            "tool_name": "weather",
            "forecast_type": "daily",
            "forecast_day_offset": day_offset,
            "forecast_day_label": "tomorrow" if day_offset == 1 else f"in {day_offset} days",
            "forecast_date": forecast_date,
            "location": resolved.as_dict(),
            "temperature_max_c": temperature_max_c,
            "temperature_min_c": temperature_min_c,
            "apparent_temperature_max_c": apparent_temperature_max_c,
            "apparent_temperature_min_c": apparent_temperature_min_c,
            "precipitation_probability_max": precipitation_probability_max,
            "wind_speed_kmh": wind_speed_kmh,
            "weather_code": weather_code,
            "weather_description": self.weather_codes.get(weather_code, "unknown conditions"),
            "source": "open-meteo",
        }

    def _resolve_weather_location(
        self,
        *,
        location: str | None,
        location_payload: dict[str, Any] | None,
    ) -> ResolvedLocation:
        clean_location = " ".join(str(location or "").split()).strip()
        if clean_location:
            return self.resolver.resolve(clean_location)

        candidate = location_payload if isinstance(location_payload, dict) else {}
        try:
            latitude = float(candidate.get("lat"))
            longitude = float(candidate.get("lng"))
        except (TypeError, ValueError):
            raise LiveToolError("A location is required.") from None
        timezone_name = " ".join(str(candidate.get("timezone") or "").split()).strip() or "auto"
        label = "your location"
        return ResolvedLocation(
            query=label,
            name=label,
            latitude=latitude,
            longitude=longitude,
            country="",
            timezone=timezone_name,
        )

    def _extract_day_offset(self, prompt: str) -> int:
        lowered = " ".join(str(prompt or "").split()).strip().lower()
        if not lowered:
            return 0
        if any(marker in lowered for marker in ("tomorrow", "утре")):
            return 1
        in_days_match = re.search(r"\b(?:in|след)\s+(\d+)\s+(?:days|day|дни|ден)\b", lowered)
        if in_days_match:
            try:
                return max(0, int(in_days_match.group(1)))
            except ValueError:
                return 0
        return 0

    def _fetch_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "HelloAgain/1.0 (weather-tool)"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw_response = response.read()
        except Exception as exc:
            raise LiveToolError(f"Failed to fetch weather data: {exc}") from exc
        try:
            return json.loads(raw_response.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise LiveToolError("Weather service returned invalid JSON.") from exc


class TimeService:
    def __init__(self, *, resolver: LocationResolver | None = None) -> None:
        self.resolver = resolver or LocationResolver()

    def get_current_time(self, location: str) -> dict[str, Any]:
        resolved = self.resolver.resolve(location)
        try:
            timezone = ZoneInfo(resolved.timezone)
        except Exception as exc:
            raise LiveToolError(f"Unsupported timezone '{resolved.timezone}'.") from exc

        local_now = dt.datetime.now(timezone)
        offset = local_now.utcoffset() or dt.timedelta()
        offset_hours = offset.total_seconds() / 3600
        return {
            "tool_name": "time",
            "location": resolved.as_dict(),
            "timezone": resolved.timezone,
            "local_time_iso": local_now.isoformat(),
            "local_time": local_now.strftime("%Y-%m-%d %H:%M:%S"),
            "local_date": local_now.strftime("%Y-%m-%d"),
            "local_clock": local_now.strftime("%H:%M"),
            "weekday": local_now.strftime("%A"),
            "utc_offset_hours": offset_hours,
            "source": "zoneinfo",
        }


class SearchService:
    base_url = "https://api.duckduckgo.com/"

    def search(self, query: str) -> dict[str, Any]:
        clean_query = " ".join(str(query or "").split()).strip()
        if not clean_query:
            raise LiveToolError("A search query is required.")

        params = {
            "q": clean_query,
            "format": "json",
            "no_redirect": "1",
            "no_html": "1",
            "skip_disambig": "0",
        }
        payload = self._fetch_json(f"{self.base_url}?{urllib.parse.urlencode(params)}")
        results = self._extract_related_topics(payload.get("RelatedTopics"))
        return {
            "tool_name": "search",
            "query": clean_query,
            "heading": str(payload.get("Heading") or "").strip(),
            "abstract": str(payload.get("AbstractText") or "").strip(),
            "answer": str(payload.get("Answer") or "").strip(),
            "answer_type": str(payload.get("AnswerType") or "").strip(),
            "results": results[:5],
            "source": "duckduckgo_instant_answer",
        }

    def _extract_related_topics(self, raw_topics: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not isinstance(raw_topics, list):
            return items
        for item in raw_topics:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("Topics"), list):
                items.extend(self._extract_related_topics(item.get("Topics")))
                continue
            text = str(item.get("Text") or "").strip()
            first_url = str(item.get("FirstURL") or "").strip()
            if not text and not first_url:
                continue
            items.append(
                {
                    "text": text,
                    "url": first_url,
                }
            )
        return items

    def _fetch_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "HelloAgain/1.0 (search-tool)"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw_response = response.read()
        except Exception as exc:
            raise LiveToolError(f"Failed to fetch search data: {exc}") from exc
        try:
            return json.loads(raw_response.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise LiveToolError("Search service returned invalid JSON.") from exc
