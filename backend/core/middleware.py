import json

from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.http import Http404, JsonResponse


class ApiJsonErrorsMiddleware:
    """
    Ensure API callers receive JSON errors instead of HTML debug pages.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            response = self.get_response(request)
        except Http404 as exc:
            return self._json_error(request, 404, "Not found.", details={"exception": str(exc)})
        except PermissionDenied as exc:
            return self._json_error(request, 403, "Permission denied.", details={"exception": str(exc)})
        except SuspiciousOperation as exc:
            return self._json_error(request, 400, "Bad request.", details={"exception": str(exc)})
        except Exception as exc:
            return self._json_error(request, 500, "Internal server error.", details={"exception": str(exc)})

        if (
            request.path.startswith("/api/")
            and response.status_code >= 400
            and "application/json" not in response.get("Content-Type", "")
        ):
            details = {}
            content = getattr(response, "content", b"")
            if content:
                try:
                    details["raw"] = content.decode("utf-8", errors="ignore")[:500]
                except Exception:
                    pass
            return self._json_error(
                request,
                response.status_code,
                response.reason_phrase or "Request failed.",
                details=details or None,
            )

        return response

    def _json_error(self, request, status, message, details=None):
        if not request.path.startswith("/api/"):
            payload = {
                400: "Bad Request",
                403: "Forbidden",
                404: "Not Found",
                500: "Internal Server Error",
            }
            fallback = payload.get(status, "Request failed")
            return JsonResponse({"error": fallback}, status=status)

        payload = {"error": message}
        if details:
            payload["details"] = details
        return JsonResponse(payload, status=status)
