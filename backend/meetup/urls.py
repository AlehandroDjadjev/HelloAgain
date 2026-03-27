from django.urls import path
from .views import (
    RecommendMeetupView,
    meetup_invites_collection,
    propose_friend_meetup,
    respond_meetup_invite,
)

urlpatterns = [
    path('recommend/', RecommendMeetupView.as_view(), name='recommend_meetup'),
    path('friends/propose/', propose_friend_meetup, name='meetup_friend_propose'),
    path('friends/invites/', meetup_invites_collection, name='meetup_invites_collection'),
    path('friends/invites/<int:invite_id>/respond/', respond_meetup_invite, name='meetup_invite_respond'),
]
