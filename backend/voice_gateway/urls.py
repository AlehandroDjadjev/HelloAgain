from django.urls import path

from . import views

urlpatterns = [
    path("live-test/", views.live_test_view, name="live_test"),
    path("conversation/", views.conversation_view, name="conversation"),
    path("transcribe/", views.transcribe_view, name="transcribe"),
    path("speak/", views.speak_view, name="speak"),
    path("get-response/", views.get_response_view, name="get_response"),
    path("health/", views.health_view, name="health"),
]
