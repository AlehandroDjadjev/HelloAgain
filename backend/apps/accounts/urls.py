from django.urls import path

from . import views


urlpatterns = [
    path("register/", views.register_view, name="accounts_register"),
    path("login/", views.login_view, name="accounts_login"),
    path("logout/", views.logout_view, name="accounts_logout"),
    path("me/", views.me_view, name="accounts_me"),
    path("agent/profile/update/", views.agent_profile_update_view, name="accounts_agent_profile_update"),
    path("agent/connections/find/", views.agent_find_connection_view, name="accounts_agent_find_connection"),
    path("agent/users/<int:user_id>/widget/", views.agent_user_widget_view, name="accounts_agent_user_widget"),
    path(
        "onboarding/questions/preview/",
        views.onboarding_questions_preview,
        name="accounts_onboarding_preview",
    ),
    path("discovery/", views.discovery_feed, name="accounts_discovery"),
    path("discovery/query/", views.discovery_query, name="accounts_discovery_query"),
    path("search/", views.search_users, name="accounts_search"),
    path("friends/", views.friends_list, name="accounts_friends"),
    path("users/<int:user_id>/", views.user_detail, name="accounts_user_detail"),
    path(
        "friend-requests/",
        views.friend_requests_collection,
        name="accounts_friend_requests",
    ),
    path(
        "friend-requests/<int:request_id>/respond/",
        views.respond_to_friend_request,
        name="accounts_friend_request_respond",
    ),
    path("contacts/", views.contacts_collection, name="accounts_contacts"),
    path("contacts/import/", views.import_contacts_view, name="accounts_contacts_import"),
    path("activities/", views.activity_event_collection, name="accounts_activities"),
]
