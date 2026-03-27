from django.urls import path
from .views import add_action_view, conversation_view, fetch_action_view, home_view, qwen_health_view, reset_state_view, state_view

urlpatterns = [
    path("", home_view, name="home"),
    path("api/add-action/", add_action_view, name="add_action"),
    path("api/fetch-action/", fetch_action_view, name="fetch_action"),
    path("api/conversation/", conversation_view, name="conversation"),
    path("api/state/", state_view, name="state"),
    path("api/reset-state/", reset_state_view, name="reset_state"),
    path("api/qwen-health/", qwen_health_view, name="qwen_health"),
]
