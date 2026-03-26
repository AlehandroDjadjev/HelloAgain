import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from voice_gateway.domain.contracts import VoiceGatewayRequest, BackendSpeakRequest
from voice_gateway.services.gateway import gateway_core

@csrf_exempt
def interact_view(request):
    """
    Entry point for user-initiated speech.
    """
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            vg_request = VoiceGatewayRequest(
                user_id=data.get("user_id", "anonymous"),
                session_id=data.get("session_id", "default_session"),
                message=data.get("message", "")
            )
            vg_response = gateway_core.process_user_request(vg_request)

            return JsonResponse({
                "status": vg_response.status,
                "spoken_text": vg_response.spoken_text,
                "structured_data": vg_response.structured_data
            })
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse({"error": "Method not allowed"}, status=405)


@csrf_exempt
def agent_speak_view(request):
    """
    Entry point for backend systems to PUSH speech to the user.
    """
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            speak_request = BackendSpeakRequest(
                user_id=data.get("user_id", "anonymous"),
                session_id=data.get("session_id", "default_session"),
                agent_name=data.get("agent_name", "UnknownAgent"),
                raw_data=data.get("raw_data", {})
            )
            vg_response = gateway_core.process_agent_request(speak_request)

            return JsonResponse({
                "status": vg_response.status,
                "spoken_text": vg_response.spoken_text,
                "structured_data": vg_response.structured_data
            })
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse({"error": "Method not allowed"}, status=405)
