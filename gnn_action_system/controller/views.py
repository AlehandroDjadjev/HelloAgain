import json
import sys

from django.http import HttpRequest, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from engine.graph_service import GraphService
from engine.qwen_worker_client import QwenWorkerClient

service = GraphService()
qwen_client = QwenWorkerClient()


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
        result = service.add_action_flow(prompt)
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
        result = service.fetch_action_flow(prompt)
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
        result = service.conversation_flow(prompt)
    except Exception as exc:
        print(f"[controller] conversation failed: {exc}", file=sys.stderr, flush=True)
        return JsonResponse({"detail": str(exc)}, status=500)
    print("[controller] conversation request completed", file=sys.stderr, flush=True)
    return JsonResponse(result)


def state_view(request: HttpRequest):
    return JsonResponse(service.export_state())


@csrf_exempt
def reset_state_view(request: HttpRequest):
    if request.method != "POST":
        return JsonResponse({"detail": "POST required"}, status=405)
    try:
        result = service.reset_state()
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
