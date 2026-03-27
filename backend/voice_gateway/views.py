import base64
import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from voice_gateway.domain.contracts import VoiceConversationRequest
from voice_gateway.services.gateway import gateway_core
from voice_gateway.services.providers import ProviderNotReadyError


def _parse_request_payload(request):
    if request.content_type and request.content_type.startswith("application/json"):
        data = json.loads(request.body or "{}")
        audio_bytes = None
        audio_base64 = str(data.get("audio_base64") or "").strip()
        if audio_base64:
            try:
                audio_bytes = base64.b64decode(audio_base64, validate=True)
            except Exception as exc:
                raise ValueError(f"Invalid audio_base64 payload: {exc}") from exc
        return data, audio_bytes

    data = {
        "user_id": request.POST.get("user_id", "anonymous"),
        "session_id": request.POST.get("session_id", "default_session"),
        "message": request.POST.get("message", ""),
        "text": request.POST.get("text", ""),
        "language": request.POST.get("language"),
    }
    audio_bytes = request.FILES["audio"].read() if "audio" in request.FILES else None
    return data, audio_bytes


def _provider_error_response(exc):
    return JsonResponse(
        {
            "error": str(exc),
            "providers": gateway_core.health_status(),
        },
        status=503,
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
            message=data.get("message", ""),
            language=data.get("language"),
        )

        response = gateway_core.process_turn(voice_request, audio_bytes=audio_bytes)
        return JsonResponse(response.to_api_dict())
    except ProviderNotReadyError as exc:
        return _provider_error_response(exc)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)


@csrf_exempt
def transcribe_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        print("[voice_gateway] transcribe request received", flush=True)
        data, audio_bytes = _parse_request_payload(request)
        if not audio_bytes:
            return JsonResponse({"error": "Audio is required."}, status=400)

        transcription = gateway_core.stt_provider.transcribe(
            audio_bytes,
            language=data.get("language"),
        )
        return JsonResponse(
            {
                "status": "success",
                "transcript": transcription.text,
                "provider": transcription.source,
                "warnings": transcription.warnings,
            }
        )
    except ProviderNotReadyError as exc:
        return _provider_error_response(exc)
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
        text = (data.get("message") or data.get("text") or "").strip()
        if not text:
            return JsonResponse({"error": "Text is required."}, status=400)

        synthesis = gateway_core.tts_provider.synthesize(text)
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
