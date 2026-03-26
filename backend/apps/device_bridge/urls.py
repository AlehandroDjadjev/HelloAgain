from django.urls import path

from .views import DeviceHeartbeatView, ScreenStateIngestView

urlpatterns = [
    path("screen-state/", ScreenStateIngestView.as_view(), name="device-screen-state"),
    path("heartbeat/", DeviceHeartbeatView.as_view(), name="device-heartbeat"),
]
