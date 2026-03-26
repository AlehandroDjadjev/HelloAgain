from django.urls import path
from . import views

urlpatterns = [
    # Profile & Management
    path("elders/", views.elders_collection, name="elders_collection"),
    path("profile/<int:elder_id>/", views.profile_detail, name="profile_detail"),
    path("profile/<int:elder_id>/edit/", views.update_profile, name="update_profile"),
    path("profile/<int:elder_id>/features/", views.update_features, name="update_features"),
    
    # Matching & Recommendations
    path("compare/", views.compare_users, name="compare_users"),
    path("find-friends/<int:elder_id>/", views.find_friends, name="find_friends"),
    
    # Model & Diagnostics
    path("train/", views.train_model, name="train_model"),
    path("health/", views.health_status, name="health_status"),
    path("schema/", views.feature_schema, name="feature_schema"),
]
