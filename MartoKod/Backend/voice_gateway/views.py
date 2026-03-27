import base64
import json

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from voice_gateway.domain.contracts import VoiceConversationRequest
from voice_gateway.services.gateway import gateway_core
from voice_gateway.services.providers import ProviderNotReadyError


def _parse_request_payload(request):
    content_type = request.content_type or ""

    if content_type.startswith("application/json"):
        payload = json.loads(request.body or "{}")
        audio_base64 = (
            payload.get("audio_base64")
            or payload.get("audioBase64")
            or payload.get("audio_b64")
        )
        if not audio_base64 and "audio" in payload and not any(
            payload.get(key)
            for key in ("message", "text", "prompt")
        ):
            audio_base64 = payload.get("audio")

        audio_bytes = None
        if audio_base64:
            try:
                encoded_audio = str(audio_base64)
                if "," in encoded_audio and encoded_audio.split(",", 1)[0].startswith("data:"):
                    encoded_audio = encoded_audio.split(",", 1)[1]
                audio_bytes = base64.b64decode(encoded_audio, validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError("Invalid base64 audio payload.") from exc

        return (
            {
                "user_id": payload.get("user_id", "anonymous"),
                "session_id": payload.get("session_id", "default_session"),
                "message": payload.get("message", ""),
                "text": payload.get("text", ""),
                "prompt": payload.get("prompt", ""),
                "language": payload.get("language"),
                "audio_mime_type": payload.get("audio_mime_type")
                or payload.get("audioMimeType")
                or payload.get("mime_type"),
                "response_format": payload.get("response_format", ""),
            },
            audio_bytes,
        )

    if content_type.startswith("audio/"):
        return (
            {
                "user_id": request.GET.get("user_id", "anonymous"),
                "session_id": request.GET.get("session_id", "default_session"),
                "message": request.GET.get("message", ""),
                "text": request.GET.get("text", ""),
                "prompt": request.GET.get("prompt", ""),
                "language": request.GET.get("language"),
                "audio_mime_type": content_type,
                "response_format": request.GET.get("response_format", ""),
            },
            request.body,
        )

    data = {
        "user_id": request.POST.get("user_id", "anonymous"),
        "session_id": request.POST.get("session_id", "default_session"),
        "message": request.POST.get("message", ""),
        "text": request.POST.get("text", ""),
        "prompt": request.POST.get("prompt", ""),
        "language": request.POST.get("language"),
        "audio_mime_type": "",
        "response_format": request.POST.get("response_format", ""),
    }
    audio_bytes = None
    if "audio" in request.FILES:
        audio_file = request.FILES["audio"]
        audio_bytes = audio_file.read()
        data["audio_mime_type"] = getattr(audio_file, "content_type", "") or ""
    return data, audio_bytes


def _provider_error_response(exc):
    return JsonResponse(
        {
            "error": str(exc),
            "providers": gateway_core.health_status(),
        },
        status=503,
    )


def _resolve_text_input(data):
    return (
        data.get("prompt")
        or data.get("message")
        or data.get("text")
        or ""
    ).strip()


def _wants_audio_response(request, data):
    response_format = (data.get("response_format") or "").strip().lower()
    accept_header = (request.headers.get("Accept") or "").lower()
    if response_format in {"audio", "binary", "wav"}:
        return True
    return "audio/" in accept_header and "application/json" not in accept_header


def live_test_view(request):
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    return render(
        request,
        "voice_gateway/live_test.html",
        {
            "transcribe_url": reverse("transcribe"),
            "get_response_url": reverse("get_response"),
            "speak_url": reverse("speak"),
            "health_url": reverse("health"),
        },
    )


@csrf_exempt
def conversation_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        data, audio_bytes = _parse_request_payload(request)
        voice_request = VoiceConversationRequest(
            user_id=data.get("user_id", "anonymous"),
            session_id=data.get("session_id", "default_session"),
            message=_resolve_text_input(data),
            language=data.get("language"),
        )

        response = gateway_core.process_turn(
            voice_request,
            audio_bytes=audio_bytes,
            audio_content_type=data.get("audio_mime_type"),
        )
        return JsonResponse(response.to_api_dict())
    except ProviderNotReadyError as exc:
        return _provider_error_response(exc)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)


@csrf_exempt
def transcribe_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        data, audio_bytes = _parse_request_payload(request)
        if not audio_bytes:
            return JsonResponse({"error": "Audio is required."}, status=400)

        transcription = gateway_core.transcribe_audio(
            audio_bytes,
            language=data.get("language"),
            content_type=data.get("audio_mime_type"),
        )
        return JsonResponse(
            {
                "status": "success",
                "message": transcription.text,
                "transcript": transcription.text,
                "provider": transcription.source,
                "warnings": transcription.warnings,
            }
        )
    except ProviderNotReadyError as exc:
        return _provider_error_response(exc)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)


@csrf_exempt
def speak_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        data, _ = _parse_request_payload(request)
        text = _resolve_text_input(data)
        if not text:
            return JsonResponse({"error": "Text is required."}, status=400)

        synthesis = gateway_core.speak_text(text)
        if _wants_audio_response(request, data):
            response = HttpResponse(
                synthesis.audio_bytes,
                content_type=synthesis.mime_type,
            )
            response["Content-Disposition"] = 'inline; filename="speech.wav"'
            response["X-Voice-Provider"] = synthesis.source
            return response

        return JsonResponse(
            {
                "status": "success",
                "text": text,
                "audio_base64": base64.b64encode(synthesis.audio_bytes).decode("ascii"),
                "audio_mime_type": synthesis.mime_type,
                "provider": synthesis.source,
                "warnings": synthesis.warnings,
            }
        )
    except ProviderNotReadyError as exc:
        return _provider_error_response(exc)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)


@csrf_exempt
def get_response_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        data, _ = _parse_request_payload(request)
        prompt = _resolve_text_input(data)
        if not prompt:
            return JsonResponse({"error": "Prompt is required."}, status=400)

        response = gateway_core.get_response(
            prompt=prompt,
            session_id=data.get("session_id", "default_session"),
            user_id=data.get("user_id", "anonymous"),
        )
        payload = response.to_api_dict()
        payload["prompt"] = prompt
        return JsonResponse(payload)
    except ProviderNotReadyError as exc:
        return _provider_error_response(exc)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)


def health_view(request):
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    return JsonResponse(
        {
            "status": "ok",
            "providers": gateway_core.health_status(),
        }
    )
