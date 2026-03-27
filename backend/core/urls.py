from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    
    # Platform APIs
    path("api/meetup/", include("meetup.urls")),
    
    # Agent & Device APIs
    path("api/agent/", include("apps.agent_sessions.urls")),
    path("api/agent/device/", include("apps.device_bridge.urls")),
    path("api/accounts/", include("apps.accounts.urls")),
    
    # GAT Engine
    path("api/recommendations/", include("recommendations.urls")),
]
