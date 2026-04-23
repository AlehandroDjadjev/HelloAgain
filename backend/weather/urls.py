from django.urls import path

from .views import current_weather_view


urlpatterns = [
    path("current/", current_weather_view, name="current_weather"),
]
