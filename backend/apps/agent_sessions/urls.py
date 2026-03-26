from django.urls import path

from .views import (
    ConfirmationApproveView,
    ConfirmationRejectView,
    SessionActionResultView,
    SessionApproveView,
    SessionCancelView,
    SessionCreateView,
    SessionDetailView,
    SessionIntentView,
    SessionNextStepView,
    SessionPauseView,
    SessionPendingConfirmationView,
    SessionPlanView,
    SessionResumeView,
)

urlpatterns = [
    # Session lifecycle
    path("sessions/", SessionCreateView.as_view(), name="session-create"),
    path("sessions/<uuid:session_id>/", SessionDetailView.as_view(), name="session-detail"),
    path("sessions/<uuid:session_id>/pause/", SessionPauseView.as_view(), name="session-pause"),
    path("sessions/<uuid:session_id>/resume/", SessionResumeView.as_view(), name="session-resume"),
    path("sessions/<uuid:session_id>/cancel/", SessionCancelView.as_view(), name="session-cancel"),

    # Intent & planning
    path("sessions/<uuid:session_id>/intent/", SessionIntentView.as_view(), name="session-intent"),
    path("sessions/<uuid:session_id>/plan/", SessionPlanView.as_view(), name="session-plan"),
    path("sessions/<uuid:session_id>/approve/", SessionApproveView.as_view(), name="session-approve"),

    # Execution loop
    path("sessions/<uuid:session_id>/next-step/", SessionNextStepView.as_view(), name="session-next-step"),
    path("sessions/<uuid:session_id>/action-result/", SessionActionResultView.as_view(), name="session-action-result"),

    # Confirmation
    path("sessions/<uuid:session_id>/pending-confirmation/", SessionPendingConfirmationView.as_view(), name="session-pending-confirmation"),
    path("confirmations/<uuid:confirmation_id>/approve/", ConfirmationApproveView.as_view(), name="confirmation-approve"),
    path("confirmations/<uuid:confirmation_id>/reject/", ConfirmationRejectView.as_view(), name="confirmation-reject"),
]
