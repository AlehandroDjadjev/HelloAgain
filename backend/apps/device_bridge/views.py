from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.agent_sessions.models import AgentSession
from apps.agent_sessions.services import SessionService

from .serializers import (
    DeviceScreenStateSerializer,
    HeartbeatSerializer,
    ScreenStateIngestSerializer,
)
from .services import DeviceBridgeService


class ScreenStateIngestView(APIView):
    """POST /api/agent/device/screen-state/ — Android posts a screen snapshot."""

    def post(self, request: Request) -> Response:
        ser = ScreenStateIngestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        ss = d["screen_state"]

        session = AgentSession.objects.get(pk=d["session_id"])
        record = DeviceBridgeService.ingest_screen_state(
            session=session,
            step_id=d.get("step_id", ""),
            foreground_package=ss.get("foreground_package", ""),
            window_title=ss.get("window_title", ""),
            screen_hash=ss.get("screen_hash", ""),
            is_sensitive=ss.get("is_sensitive", False),
            nodes=ss.get("nodes", []),
            captured_at=ss["captured_at"],
            focused_element_ref=ss.get("focused_element_ref", ""),
        )
        return Response(
            DeviceScreenStateSerializer(record).data,
            status=status.HTTP_201_CREATED,
        )


class DeviceHeartbeatView(APIView):
    """
    POST /api/agent/device/heartbeat/

    Mobile device keeps the session alive during execution.
    Returns session liveness and the expected step index so the device can
    detect drift between its local counter and the server's.
    """

    def post(self, request: Request) -> Response:
        ser = HeartbeatSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data

        try:
            session = AgentSession.objects.get(pk=d["session_id"])
        except AgentSession.DoesNotExist:
            return Response(
                {"detail": "Session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        result = SessionService.heartbeat(
            session=session,
            current_step=d["current_step"],
            foreground_package=d["foreground_package"],
        )
        return Response(result)
