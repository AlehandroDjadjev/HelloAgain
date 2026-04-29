from __future__ import annotations

import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.accounts.services import profile_for_token

from .services import extract_weather_day_offset, get_weather_snapshot


logger = logging.getLogger(__name__)


def _json_ok(data: dict, status_code: int = 200) -> JsonResponse:
    return JsonResponse(data, status=status_code)


def _json_error(message: str, status_code: int = 400, code: str | None = None) -> JsonResponse:
    payload = {"status": "error", "message": message}
    if code:
        payload["code"] = code
    return JsonResponse(payload, status=status_code)


def _parse_body(request) -> dict:
    if not request.body:
        return {}
    return json.loads(request.body)


def _token_from_request(request) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("token "):
        return header.split(None, 1)[1].strip()
    return None


@csrf_exempt
@require_http_methods(["POST"])
def current_weather_view(request):
    try:
        body = _parse_body(request)
    except Exception:
        return _json_error("Invalid JSON body.")

    location = body.get("location") if isinstance(body.get("location"), dict) else body
    timezone_name = str(body.get("timezone") or "").strip() or None
    prompt = str(body.get("prompt") or "").strip()
    day_offset = body.get("day_offset")

    lat = location.get("lat") if isinstance(location, dict) else None
    lng = location.get("lng") if isinstance(location, dict) else None
    logger.info("weather endpoint received location lat=%s lng=%s", lat, lng)

    if lat is None or lng is None:
        profile = profile_for_token(_token_from_request(request))
        if profile is not None and profile.home_lat is not None and profile.home_lng is not None:
            lat = profile.home_lat
            lng = profile.home_lng

    if lat is None or lng is None:
        return _json_error(
            "Location is required. Send location.lat/location.lng or use an account with stored home coordinates.",
            code="LOCATION_REQUIRED",
        )

    try:
        resolved_day_offset = extract_weather_day_offset(prompt)
        if day_offset is not None:
            resolved_day_offset = max(0, int(day_offset))
        payload = get_weather_snapshot(
            lat=float(lat),
            lng=float(lng),
            day_offset=resolved_day_offset,
            timezone_name=timezone_name,
        )
        logger.info("weather endpoint success widget_type=%s", payload.get("widget_type"))
    except ValueError as exc:
        return _json_error(str(exc), code="INVALID_LOCATION")
    except Exception as exc:
        logger.warning("weather endpoint failed: %s", exc, exc_info=True)
        return _json_error(f"Weather lookup failed: {exc}", status_code=502, code="WEATHER_UNAVAILABLE")
    return _json_ok({"status": "success", **payload})

