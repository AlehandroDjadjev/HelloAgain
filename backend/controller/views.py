import json
import sys
from functools import lru_cache

from django.http import HttpRequest, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from engine.qwen_worker_client import QwenWorkerClient
from engine.semi_agent_service import SemiAgentService

qwen_client = QwenWorkerClient()
semi_agent_service = SemiAgentService(qwen_client=qwen_client)


@lru_cache(maxsize=1)
def _graph_service():
    from engine.graph_service import GraphService

    return GraphService()


def _parse_json_request(request: HttpRequest) -> dict:
    if not request.body:
        return {}
    return json.loads(request.body.decode("utf-8"))


def _base_url(request: HttpRequest) -> str:
    return request.build_absolute_uri("/").rstrip("/")


def _user_payload_kwargs(payload: dict | None) -> dict:
    source = payload if isinstance(payload, dict) else {}
    return {
        "user_id": str(source.get("user_id") or "").strip() or None,
        "phone_number": str(source.get("phone_number") or "").strip() or None,
    }


def home_view(request: HttpRequest):
    return render(request, "controller/home.html")


@csrf_exempt
def add_action_view(request: HttpRequest):
    if request.method != "POST":
        return JsonResponse({"detail": "POST required"}, status=405)
    print("[controller] add-action request received", file=sys.stderr, flush=True)
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except json.JSONDecodeError as exc:
        return JsonResponse({"detail": f"invalid JSON body: {exc}"}, status=400)
    prompt = payload.get("prompt", "")
    if not prompt:
        return JsonResponse({"detail": "prompt required"}, status=400)
    try:
        result = _graph_service().add_action_flow(prompt, **_user_payload_kwargs(payload))
    except ValueError as exc:
        print(f"[controller] add-action rejected: {exc}", file=sys.stderr, flush=True)
        return JsonResponse({"detail": str(exc)}, status=400)
    except Exception as exc:
        print(f"[controller] add-action failed: {exc}", file=sys.stderr, flush=True)
        return JsonResponse({"detail": str(exc)}, status=500)
    print("[controller] add-action request completed", file=sys.stderr, flush=True)
    return JsonResponse(result)


@csrf_exempt
def fetch_action_view(request: HttpRequest):
    if request.method != "POST":
        return JsonResponse({"detail": "POST required"}, status=405)
    print("[controller] fetch-action request received", file=sys.stderr, flush=True)
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except json.JSONDecodeError as exc:
        return JsonResponse({"detail": f"invalid JSON body: {exc}"}, status=400)
    prompt = payload.get("prompt", "")
    if not prompt:
        return JsonResponse({"detail": "prompt required"}, status=400)
    try:
        result = _graph_service().fetch_action_flow(prompt, **_user_payload_kwargs(payload))
    except Exception as exc:
        print(f"[controller] fetch-action failed: {exc}", file=sys.stderr, flush=True)
        return JsonResponse({"detail": str(exc)}, status=500)
    print("[controller] fetch-action request completed", file=sys.stderr, flush=True)
    return JsonResponse(result)


@csrf_exempt
def conversation_view(request: HttpRequest):
    if request.method != "POST":
        return JsonResponse({"detail": "POST required"}, status=405)
    print("[controller] conversation request received", file=sys.stderr, flush=True)
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except json.JSONDecodeError as exc:
        return JsonResponse({"detail": f"invalid JSON body: {exc}"}, status=400)
    prompt = payload.get("prompt", "")
    if not prompt:
        return JsonResponse({"detail": "prompt required"}, status=400)
    try:
        result = _graph_service().conversation_flow(prompt, **_user_payload_kwargs(payload))
    except Exception as exc:
        print(f"[controller] conversation failed: {exc}", file=sys.stderr, flush=True)
        return JsonResponse({"detail": str(exc)}, status=500)
    print("[controller] conversation request completed", file=sys.stderr, flush=True)
    return JsonResponse(result)


def state_view(request: HttpRequest):
    return JsonResponse(
        _graph_service().export_state(
            user_id=request.GET.get("user_id"),
            phone_number=request.GET.get("phone_number"),
        )
    )


@csrf_exempt
def reset_state_view(request: HttpRequest):
    if request.method != "POST":
        return JsonResponse({"detail": "POST required"}, status=405)
    try:
        payload = _parse_json_request(request)
    except json.JSONDecodeError as exc:
        return JsonResponse({"detail": f"invalid JSON body: {exc}"}, status=400)
    try:
        result = _graph_service().reset_state(
            **_user_payload_kwargs(payload),
            reset_all=bool(payload.get("reset_all")),
        )
    except Exception as exc:
        return JsonResponse({"detail": str(exc)}, status=500)
    return JsonResponse(result)


def qwen_health_view(request: HttpRequest):
    if request.method != "GET":
        return JsonResponse({"detail": "GET required"}, status=405)
    try:
        payload = qwen_client.health()
    except Exception as exc:
        return JsonResponse({"status": "error", "detail": str(exc)}, status=503)
    return JsonResponse(payload)


def agent_mcp_registry_view(request: HttpRequest):
    if request.method != "GET":
        return JsonResponse({"detail": "GET required"}, status=405)
    try:
        payload = semi_agent_service.get_registry_payload(base_url=_base_url(request))
    except Exception as exc:
        return JsonResponse({"detail": str(exc)}, status=500)
    return JsonResponse(payload)


def agent_mcp_descriptor_view(request: HttpRequest, mcp_id: str):
    if request.method != "GET":
        return JsonResponse({"detail": "GET required"}, status=405)
    try:
        payload = semi_agent_service.get_descriptor_payload(mcp_id, base_url=_base_url(request))
    except FileNotFoundError:
        return JsonResponse({"detail": f"Unknown MCP '{mcp_id}'."}, status=404)
    except Exception as exc:
        return JsonResponse({"detail": str(exc)}, status=500)
    return JsonResponse(payload)


@csrf_exempt
def agent_mcp_invoke_view(request: HttpRequest, mcp_id: str):
    if request.method != "POST":
        return JsonResponse({"detail": "POST required"}, status=405)
    try:
        payload = _parse_json_request(request)
    except json.JSONDecodeError as exc:
        return JsonResponse({"detail": f"invalid JSON body: {exc}"}, status=400)

    tool_name = payload.get("tool_name", "")
    arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
    fallback_prompt = payload.get("prompt", "")
    try:
        result = semi_agent_service.invoke_mcp(
            mcp_id=mcp_id,
            tool_name=tool_name,
            arguments=arguments,
            fallback_prompt=fallback_prompt,
            user_id=str(payload.get("user_id") or payload.get("phone_number") or "anonymous"),
            board_state=payload.get("board_state") if isinstance(payload.get("board_state"), dict) else {},
        )
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"detail": str(exc)}, status=500)
    return JsonResponse(result)


def agent_board_memory_view(request: HttpRequest):
    if request.method == "GET":
        try:
            user_id = str(request.GET.get("user_id") or "").strip() or None
            if user_id:
                board_state = semi_agent_service.connections_service.load_board_state_for_user(
                    user_id
                )
                if board_state is not None:
                    payload = {"ok": True, "board_state": board_state}
                else:
                    payload = semi_agent_service.get_board_memory_state()
            else:
                payload = semi_agent_service.get_board_memory_state()
        except Exception as exc:
            return JsonResponse({"detail": str(exc)}, status=500)
        return JsonResponse(payload)

    if request.method != "POST":
        return JsonResponse({"detail": "GET or POST required"}, status=405)

    try:
        payload = _parse_json_request(request)
    except json.JSONDecodeError as exc:
        return JsonResponse({"detail": f"invalid JSON body: {exc}"}, status=400)

    board_state = payload.get("board_state")
    if not isinstance(board_state, dict):
        return JsonResponse({"detail": "board_state object required"}, status=400)

    try:
        user_id = str(payload.get("user_id") or "").strip() or None
        removed_result_id = str(payload.get("removed_result_id") or "").strip() or None
        if user_id:
            if removed_result_id:
                semi_agent_service.board_memory.remove_result_binding(removed_result_id)
            persisted_state = semi_agent_service.connections_service.save_board_state_for_user(
                user_id,
                board_state,
            )
            if persisted_state is not None:
                result = {"ok": True, "board_state": persisted_state}
            else:
                result = semi_agent_service.save_board_memory_state(
                    board_state=board_state,
                    removed_result_id=removed_result_id,
                )
        else:
            result = semi_agent_service.save_board_memory_state(
                board_state=board_state,
                removed_result_id=removed_result_id,
            )
    except Exception as exc:
        return JsonResponse({"detail": str(exc)}, status=500)
    return JsonResponse(result)


@csrf_exempt
def agent_run_view(request: HttpRequest):
    if request.method != "POST":
        return JsonResponse({"detail": "POST required"}, status=405)
    try:
        payload = _parse_json_request(request)
    except json.JSONDecodeError as exc:
        return JsonResponse({"detail": f"invalid JSON body: {exc}"}, status=400)

    prompt = payload.get("prompt", "")
    if not str(prompt).strip():
        return JsonResponse({"detail": "prompt required"}, status=400)

    try:
        result = semi_agent_service.run(
            prompt=str(prompt),
            board_state=payload.get("board_state") if isinstance(payload.get("board_state"), dict) else {},
            largest_empty_space=payload.get("largest_empty_space")
            if isinstance(payload.get("largest_empty_space"), dict)
            else {},
            user_id=str(payload.get("user_id") or payload.get("phone_number") or "anonymous"),
            session_id=str(payload.get("session_id") or "default_session"),
            reasoning_provider=str(payload.get("reasoning_provider") or "openai"),
        )
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    except Exception as exc:
        print(f"[controller] semi-agent failed: {exc}", file=sys.stderr, flush=True)
        return JsonResponse({"detail": str(exc)}, status=500)
    return JsonResponse(result)


@csrf_exempt
def agent_run_start_view(request: HttpRequest):
    if request.method != "POST":
        return JsonResponse({"detail": "POST required"}, status=405)
    print("[controller] semi-agent start request received", file=sys.stderr, flush=True)
    try:
        payload = _parse_json_request(request)
    except json.JSONDecodeError as exc:
        return JsonResponse({"detail": f"invalid JSON body: {exc}"}, status=400)

    prompt = payload.get("prompt", "")
    if not str(prompt).strip():
        return JsonResponse({"detail": "prompt required"}, status=400)

    try:
        result = semi_agent_service.start_run(
            prompt=str(prompt),
            board_state=payload.get("board_state") if isinstance(payload.get("board_state"), dict) else {},
            largest_empty_space=payload.get("largest_empty_space")
            if isinstance(payload.get("largest_empty_space"), dict)
            else {},
            user_id=str(payload.get("user_id") or payload.get("phone_number") or "anonymous"),
            session_id=str(payload.get("session_id") or "default_session"),
            reasoning_provider=str(payload.get("reasoning_provider") or "openai"),
        )
    except ValueError as exc:
        print(f"[controller] semi-agent start rejected: {exc}", file=sys.stderr, flush=True)
        return JsonResponse({"detail": str(exc)}, status=400)
    except Exception as exc:
        print(f"[controller] semi-agent start failed: {exc}", file=sys.stderr, flush=True)
        return JsonResponse({"detail": str(exc)}, status=500)
    print("[controller] semi-agent start request completed", file=sys.stderr, flush=True)
    return JsonResponse(result)


def agent_run_speech_view(request: HttpRequest, run_id: str):
    if request.method != "GET":
        return JsonResponse({"detail": "GET required"}, status=405)
    try:
        result = semi_agent_service.get_run_speech(run_id)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=404)
    except Exception as exc:
        return JsonResponse({"detail": str(exc)}, status=500)
    return JsonResponse(result)


def agent_run_whitespace_view(request: HttpRequest, run_id: str):
    if request.method != "GET":
        return JsonResponse({"detail": "GET required"}, status=405)
    try:
        result = semi_agent_service.get_run_whitespace(run_id)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=404)
    except Exception as exc:
        return JsonResponse({"detail": str(exc)}, status=500)
    return JsonResponse(result)


@csrf_exempt
def agent_object_open_view(request: HttpRequest):
    if request.method != "POST":
        return JsonResponse({"detail": "POST required"}, status=405)
    try:
        payload = _parse_json_request(request)
    except json.JSONDecodeError as exc:
        return JsonResponse({"detail": f"invalid JSON body: {exc}"}, status=400)

    try:
        result = semi_agent_service.open_board_object(
            object_payload=payload.get("object") if isinstance(payload.get("object"), dict) else payload,
            user_id=str(payload.get("user_id") or payload.get("phone_number") or "anonymous"),
        )
    except Exception as exc:
        return JsonResponse({"detail": str(exc)}, status=500)
    return JsonResponse(result)


@csrf_exempt
def agent_object_delete_view(request: HttpRequest):
    if request.method != "POST":
        return JsonResponse({"detail": "POST required"}, status=405)
    try:
        payload = _parse_json_request(request)
    except json.JSONDecodeError as exc:
        return JsonResponse({"detail": f"invalid JSON body: {exc}"}, status=400)

    try:
        result = semi_agent_service.delete_board_object(
            object_payload=payload.get("object")
            if isinstance(payload.get("object"), dict)
            else payload,
            user_id=str(payload.get("user_id") or payload.get("phone_number") or "anonymous"),
        )
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"detail": str(exc)}, status=500)
    return JsonResponse(result)
