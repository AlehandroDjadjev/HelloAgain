from django.urls import path

from . import views

urlpatterns = [
    path("conversation/", views.conversation_view, name="conversation"),
    path("transcribe/", views.transcribe_view, name="transcribe"),
    path("speak/", views.speak_view, name="speak"),
    path("health/", views.health_view, name="health"),
]
