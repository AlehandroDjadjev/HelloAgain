from django.urls import path

from . import views

urlpatterns = [
    path("conversation/", views.conversation_view, name="conversation"),
    path("interact/", views.interact_view, name="interact"),
    path("agent-speak/", views.agent_speak_view, name="agent-speak"),
    path("transcribe/", views.transcribe_view, name="transcribe"),
    path("speak/", views.speak_view, name="speak"),
    path("health/", views.health_view, name="health"),
]
