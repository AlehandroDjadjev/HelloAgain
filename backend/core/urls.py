from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/voice-gateway/", include("voice_gateway.urls")),
    path("api/agent/", include("apps.agent_sessions.urls")),
    path("api/agent/device/", include("apps.device_bridge.urls")),
    path("api/meetup/", include("meetup.urls")),
    path("api/accounts/", include("apps.accounts.urls")),
]
