from django.urls import path
from . import views

urlpatterns = [
    path('interact/', views.interact_view, name='interact'),
    path('agent-speak/', views.agent_speak_view, name='agent-speak'),
]
