from __future__ import annotations

from urllib.parse import urlparse

from django.http import HttpResponse


_LOCAL_DEV_HOSTS = {"localhost", "127.0.0.1", "::1"}
_ALLOWED_METHODS = "GET, POST, OPTIONS"
_ALLOWED_HEADERS = "Accept, Content-Type, Origin, X-Requested-With"


def _append_vary(existing: str | None, value: str) -> str:
    if not existing:
        return value
    parts = [item.strip() for item in existing.split(",") if item.strip()]
    if value not in parts:
        parts.append(value)
    return ", ".join(parts)


def _origin_is_allowed(origin: str) -> bool:
    if not origin:
        return False

    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"}:
        return False

    hostname = (parsed.hostname or "").lower()
    return hostname in _LOCAL_DEV_HOSTS


class LocalDevCorsMiddleware:
    """Allow the Flutter web dev server to call the local Django API."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        origin = request.headers.get("Origin", "")
        allow_origin = _origin_is_allowed(origin)

        if request.method == "OPTIONS" and allow_origin:
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)

        if allow_origin:
            response["Access-Control-Allow-Origin"] = origin
            response["Access-Control-Allow-Methods"] = _ALLOWED_METHODS
            response["Access-Control-Allow-Headers"] = _ALLOWED_HEADERS
            response["Access-Control-Max-Age"] = "86400"
            response["Vary"] = _append_vary(response.get("Vary"), "Origin")

        return response
