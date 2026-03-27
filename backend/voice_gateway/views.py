import base64
import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from voice_gateway.domain.contracts import (
    BackendSpeakRequest,
    VoiceConversationRequest,
    VoiceGatewayRequest,
)
from voice_gateway.services.gateway import gateway_core
from voice_gateway.services.providers import ProviderNotReadyError


def _parse_request_payload(request):
    if request.content_type and request.content_type.startswith("application/json"):
        return json.loads(request.body or "{}"), None

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
def interact_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        data, _ = _parse_request_payload(request)
        gateway_request = VoiceGatewayRequest(
            user_id=data.get("user_id", "anonymous"),
            session_id=data.get("session_id", "default_session"),
            message=data.get("message", ""),
        )
        response = gateway_core.process_user_request(gateway_request)
        return JsonResponse(
            {
                "status": response.status,
                "spoken_text": response.spoken_text,
                "structured_data": response.structured_data,
            }
        )
    except ProviderNotReadyError as exc:
        return _provider_error_response(exc)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)


@csrf_exempt
def agent_speak_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        data, _ = _parse_request_payload(request)
        speak_request = BackendSpeakRequest(
            user_id=data.get("user_id", "anonymous"),
            session_id=data.get("session_id", "default_session"),
            agent_name=data.get("agent_name", "UnknownAgent"),
            raw_data=data.get("raw_data", {}) or {"text": data.get("text", "")},
        )
        response = gateway_core.process_agent_request(speak_request)
        return JsonResponse(
            {
                "status": response.status,
                "spoken_text": response.spoken_text,
                "structured_data": response.structured_data,
            }
        )
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
