from django.urls import path
from .views import RecommendMeetupView

urlpatterns = [
    path('recommend/', RecommendMeetupView.as_view(), name='recommend_meetup'),
]
