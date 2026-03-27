from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/voice/", include("voice_gateway.urls")),
    path("", include("controller.urls")),
]
