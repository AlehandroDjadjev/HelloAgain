"""
All agent session views.

Design rules (enforced here):
- Views are thin: validate input, call a service, return the response.
- Business logic lives exclusively in service classes.
- Every 404 / 400 / 409 is handled by Django exceptions or DRF raise_exception.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from django.http import Http404
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.agent_plans.models import ActionPlanRecord
from apps.agent_plans.serializers import ActionPlanRecordSerializer, ActionPlanSubmitSerializer
from apps.agent_plans.services import (
    IntentService,
    PlanCompiler,
    PlanService,
    PlanValidator,
    CompilationError,
)
from apps.agent_core.schemas import ActionPlan as ActionPlanSchema
from apps.agent_policy.models import UserAutomationPolicy
from apps.agent_policy.services import PolicyEnforcer
from apps.device_bridge.serializers import ActionResultIngestSerializer, AgentActionEventSerializer
from apps.device_bridge.services import DeviceBridgeService
from apps.audit_log.services import AuditService
from apps.audit_log.models import AuditEventType, AuditActor

from .execution_service import ExecutionService
from .models import AgentSession, ConfirmationRecord, SessionStatus
from .serializers import (
    ActionResultV2Serializer,
    AgentSessionCreateSerializer,
    AgentSessionDetailSerializer,
    ConfirmationRecordSerializer,
    ExecutionDecisionSerializer,
    IntentSubmitSerializer,
    NextStepRequestSerializer,
    PendingConfirmationResponseSerializer,
    SessionApproveSerializer,
    SessionCreateResponseSerializer,
)
from .services import SessionService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_session(session_id: UUID) -> AgentSession:
    try:
        return AgentSession.objects.get(pk=session_id)
    except AgentSession.DoesNotExist:
        raise Http404


def _get_plan(session: AgentSession) -> ActionPlanRecord:
    plan = PlanService.get_plan(session)
    if plan is None:
        from rest_framework.exceptions import NotFound
        raise NotFound("No plan found for this session.")
    return plan


def _user_id_from_request(request: Request) -> str:
    """
    Extract user identifier from the authenticated request.
    Returns the string PK if a real User is attached; otherwise falls back to
    the 'user_id' body field (development / unauthenticated convenience).
    """
    if request.user and request.user.is_authenticated:
        return str(request.user.pk)
    return request.data.get("user_id", "anonymous")


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

class SessionCreateView(APIView):
    """POST /api/agent/sessions/"""

    def post(self, request: Request) -> Response:
        ser = AgentSessionCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data

        session = SessionService.create(
            user_id=_user_id_from_request(request),
            device_id=d["device_id"],
            input_mode=d["input_mode"],
            supported_packages=d["supported_packages"],
        )
        return Response(
            SessionCreateResponseSerializer({
                "session_id": session.id,
                "status": session.status,
            }).data,
            status=status.HTTP_201_CREATED,
        )


class SessionDetailView(APIView):
    """GET /api/agent/sessions/{id}/"""

    def get(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)
        return Response(AgentSessionDetailSerializer(session).data)


class SessionPauseView(APIView):
    """POST /api/agent/sessions/{id}/pause/"""

    def post(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)
        try:
            session = SessionService.pause(session)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(AgentSessionDetailSerializer(session).data)


class SessionResumeView(APIView):
    """POST /api/agent/sessions/{id}/resume/"""

    def post(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)
        try:
            session = SessionService.resume(session)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(AgentSessionDetailSerializer(session).data)


class SessionCancelView(APIView):
    """POST /api/agent/sessions/{id}/cancel/"""

    def post(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)
        try:
            session = SessionService.cancel(session)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(AgentSessionDetailSerializer(session).data)


# ---------------------------------------------------------------------------
# Intent & planning
# ---------------------------------------------------------------------------

class SessionIntentView(APIView):
    """
    POST /api/agent/sessions/{id}/intent/

    Calls the LLM-powered IntentService to parse the transcript into a
    structured intent. The raw LLM response is stored in IntentRecord for
    debugging. Falls back to keyword detection if the LLM is unavailable.

    Response includes:
      - parsed intent (goal, goal_type, app_package, entities, risk_level, confidence)
      - ambiguity_flags if confidence < 0.5
      - can_auto_compile: true if a plan template exists for this intent
    """

    def post(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)
        ser = IntentSubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        transcript = ser.validated_data["transcript"]

        svc = IntentService()
        intent_result = svc.parse_intent(
            transcript=transcript,
            supported_packages=list(session.supported_packages) or None,
        )

        PlanService.store_intent(
            session=session,
            raw_transcript=transcript,
            parsed_intent=intent_result.to_dict(),
            llm_raw_response=intent_result.raw_llm_response,
            goal_type=intent_result.goal_type,
            confidence=intent_result.confidence,
            ambiguity_flags=intent_result.ambiguity_flags,
        )

        return Response(
            {
                "intent": intent_result.to_dict(),
                "can_auto_compile": PlanCompiler.has_template(
                    intent_result.goal_type, intent_result.app_package
                ),
            },
            status=status.HTTP_200_OK,
        )


class SessionPlanView(APIView):
    """
    POST /api/agent/sessions/{id}/plan/

    Two modes:
      1. auto_compile=true  (recommended) — compile a plan from the stored intent
         using the deterministic PlanCompiler template registry.
         No request body needed beyond {"auto_compile": true}.

      2. Submit a full typed plan JSON ({"plan": {...}}) — validated against
         the Pydantic ActionPlan schema before persisting.
         Used by external callers or the Flutter test UI.

    In both modes the plan is validated by PlanValidator before storage.
    """

    def post(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)

        if request.data.get("auto_compile"):
            return self._auto_compile(session, session_id)
        return self._submit_plan(session, session_id, request)

    def _auto_compile(self, session: AgentSession, session_id: UUID) -> Response:
        from apps.agent_plans.models import IntentRecord
        try:
            intent_record = IntentRecord.objects.get(session=session)
        except IntentRecord.DoesNotExist:
            return Response(
                {"detail": "No intent found for this session. POST to /intent/ first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Re-hydrate the IntentResult from the stored parsed_intent
        from apps.agent_plans.services import IntentResult
        pd = intent_record.parsed_intent
        intent_result = IntentResult(
            goal=pd.get("goal", ""),
            goal_type=intent_record.goal_type or pd.get("goal_type", ""),
            app_package=pd.get("app_package", ""),
            target_app=pd.get("target_app", ""),
            entities=pd.get("entities", {}),
            risk_level=pd.get("risk_level", "low"),
            confidence=intent_record.confidence,
            ambiguity_flags=intent_record.ambiguity_flags or [],
        )

        try:
            compiled_plan = PlanCompiler.compile(intent_result, str(session_id))
        except CompilationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        validation = PlanValidator.validate(
            compiled_plan,
            allowed_packages=list(session.supported_packages) or None,
        )
        if not validation.is_valid:
            return Response(
                {"detail": "Compiled plan failed validation.", "errors": validation.errors},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        record = PlanService.store_plan(session=session, validated_plan=compiled_plan)
        return Response(
            ActionPlanRecordSerializer(record).data,
            status=status.HTTP_201_CREATED,
        )

    def _submit_plan(self, session: AgentSession, session_id: UUID, request: Request) -> Response:
        ser = ActionPlanSubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        raw_plan = ser.validated_data["plan"]
        raw_plan.setdefault("session_id", str(session_id))
        validated = ActionPlanSchema.model_validate(raw_plan)

        validation = PlanValidator.validate(
            validated,
            allowed_packages=list(session.supported_packages) or None,
        )
        if not validation.is_valid:
            return Response(
                {"detail": "Plan failed validation.", "errors": validation.errors},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        record = PlanService.store_plan(session=session, validated_plan=validated)
        return Response(
            ActionPlanRecordSerializer(record).data,
            status=status.HTTP_201_CREATED,
        )


class SessionApproveView(APIView):
    """
    POST /api/agent/sessions/{id}/approve/

    Runs policy enforcement before marking the plan as approved.

    Flow:
      1. Load the pending plan from the DB and re-hydrate as Pydantic ActionPlan.
      2. Resolve the user's UserAutomationPolicy (or None for defaults).
      3. Call PolicyEnforcer.enforce_policy() — system rules run first, then user rules,
         then confirmation-insertion.
      4a. If blocked  → 403 with blocked_reason + policy decisions.
      4b. If modified → save modified steps back to DB, then approve.
      4c. If unchanged → approve as-is.
    """

    def post(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)
        ser = SessionApproveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        plan_record = _get_plan(session)

        # Re-hydrate the stored plan as a validated Pydantic ActionPlan
        try:
            plan = ActionPlanSchema.model_validate({
                "plan_id":     str(plan_record.id),
                "session_id":  str(session_id),
                "goal":        plan_record.goal,
                "app_package": plan_record.app_package,
                "steps":       plan_record.steps,
                "version":     plan_record.version,
            })
        except Exception as exc:
            return Response(
                {"detail": f"Stored plan is invalid: {exc}"},
                status=status.HTTP_409_CONFLICT,
            )

        # Resolve user policy (None → all system defaults apply)
        user_id = _user_id_from_request(request)
        user_policy = (
            UserAutomationPolicy.objects
            .filter(user_id=user_id)
            .order_by("-updated_at")
            .first()
        )

        # Resolve goal_type from IntentRecord if present
        goal_type = ""
        try:
            goal_type = session.intent.goal_type or ""
        except Exception:
            pass

        # Enforce policy
        result = PolicyEnforcer.enforce_policy(
            plan=plan,
            goal_type=goal_type,
            user_policy=user_policy,
            session=session,
        )

        if not result.approved:
            AuditService.record(
                session=session,
                event_type=AuditEventType.POLICY_VIOLATION,
                actor=AuditActor.SYSTEM,
                payload={
                    "plan_id": str(plan_record.id),
                    "blocked_reason": result.blocked_reason,
                    "decisions_count": len(result.policy_decisions),
                },
            )
            return Response(
                {
                    "detail": result.blocked_reason,
                    "policy_decisions": [
                        {
                            "rule": d.rule_name,
                            "decision": d.decision,
                            "reason": d.reason,
                            "action_id": d.action_id or None,
                        }
                        for d in result.policy_decisions
                        if d.decision == "block"
                    ],
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # If policy inserted confirmation steps, persist the modified step list
        if result.is_modified and result.modified_plan:
            plan_record.steps = [
                s.model_dump(mode="json")
                for s in result.modified_plan.steps
            ]
            plan_record.save(update_fields=["steps"])

        # Approve
        try:
            approved_record = PlanService.approve_plan(session=session)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)

        return Response({
            "approved": True,
            "policy_modified": result.is_modified,
            "confirmations_inserted": sum(
                1 for d in result.policy_decisions if d.decision == "confirm"
            ),
            "effective_plan": ActionPlanRecordSerializer(approved_record).data,
        })


# ---------------------------------------------------------------------------
# Execution loop
# ---------------------------------------------------------------------------

class SessionNextStepView(APIView):
    """
    POST /api/agent/sessions/{id}/next-step/

    Returns the next action for the device to execute, plus a machine-readable
    status that tells the client whether to execute, confirm, retry, etc.

    Response shape:
      {
        "next_action":    <step dict> | null,
        "status":         "execute" | "confirm" | "retry" | "complete" | "abort" | "manual_takeover",
        "executor_hint":  "whatsapp_send_message_v1",
        "reason":         "human-readable explanation"
      }
    """

    def post(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)
        ser = NextStepRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        plan = _get_plan(session)
        response = ExecutionService.get_next_action(
            session=session,
            plan=plan,
            screen_state=ser.validated_data.get("screen_state"),
            completed_action_ids=ser.validated_data.get("completed_action_ids", []),
            last_action_result=ser.validated_data.get("last_action_result"),
        )
        return Response(response.to_dict(), status=status.HTTP_200_OK)


class SessionActionResultView(APIView):
    """
    POST /api/agent/sessions/{id}/action-result/

    Android posts the result of one executed step.  The service:
      1. Persists the screen state and action event.
      2. Advances the step counter on success.
      3. Returns an execution decision: continue / confirm / retry / abort / complete.
    """

    def post(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)
        ser = ActionResultV2Serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data

        result = d["result"]
        action_id = d["action_id"]
        plan_id = d["plan_id"]

        # Persist screen state if provided.
        screen_record = None
        if d.get("screen_state"):
            ss = d["screen_state"]
            screen_record = DeviceBridgeService.ingest_screen_state(
                session=session,
                step_id=action_id,
                foreground_package=ss.get("foreground_package", ""),
                window_title=ss.get("window_title", ""),
                screen_hash=ss.get("screen_hash", ""),
                is_sensitive=ss.get("is_sensitive", False),
                nodes=ss.get("nodes", []),
                captured_at=ss.get("captured_at") or datetime.now(timezone.utc),
                focused_element_ref=ss.get("focused_element_ref", ""),
            )

        # Derive legacy status string from the new result shape.
        result_status = "success" if result["success"] else "failure"
        error_code = result.get("code", "") if not result["success"] else ""

        DeviceBridgeService.record_action_result(
            session=session,
            plan_id=plan_id,
            step_id=action_id,
            step_type=_infer_step_type(session, action_id),
            status=result_status,
            executed_at=d.get("executed_at") or datetime.now(timezone.utc),
            error_code=error_code,
            error_detail=result.get("message", ""),
            screen_state=screen_record,
            duration_ms=d.get("duration_ms", 0),
        )

        # Reload session to pick up step advancement.
        session.refresh_from_db()
        plan = _get_plan(session)
        decision = ExecutionService.decide_after_result(
            session=session,
            plan=plan,
            action_id=action_id,
            result_success=result["success"],
            result_code=result.get("code", ""),
        )
        return Response(
            ExecutionDecisionSerializer(decision.to_dict()).data,
            status=status.HTTP_200_OK,
        )


def _infer_step_type(session: AgentSession, action_id: str) -> str:
    """Look up the step type from the plan's step list by step id."""
    plan = PlanService.get_plan(session)
    if plan:
        for step in (plan.steps or []):
            if step.get("id") == action_id:
                return step.get("type", "UNKNOWN")
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------

class SessionPendingConfirmationView(APIView):
    """GET /api/agent/sessions/{id}/pending-confirmation/"""

    def get(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)
        conf = ConfirmationRecord.objects.filter(
            session=session,
            status=ConfirmationRecord.Status.PENDING,
        ).first()
        return Response(
            PendingConfirmationResponseSerializer({
                "has_pending": conf is not None,
                "confirmation": conf,
            }).data
        )


class ConfirmationApproveView(APIView):
    """POST /api/agent/confirmations/{id}/approve/"""

    def post(self, request: Request, confirmation_id: UUID) -> Response:
        try:
            conf = ConfirmationRecord.objects.select_related("session").get(pk=confirmation_id)
        except ConfirmationRecord.DoesNotExist:
            raise Http404
        try:
            resolved = SessionService.resolve_confirmation(
                confirmation_id=confirmation_id,
                approved=True,
                session=conf.session,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(ConfirmationRecordSerializer(resolved).data)


class ConfirmationRejectView(APIView):
    """POST /api/agent/confirmations/{id}/reject/"""

    def post(self, request: Request, confirmation_id: UUID) -> Response:
        try:
            conf = ConfirmationRecord.objects.select_related("session").get(pk=confirmation_id)
        except ConfirmationRecord.DoesNotExist:
            raise Http404
        try:
            resolved = SessionService.resolve_confirmation(
                confirmation_id=confirmation_id,
                approved=False,
                session=conf.session,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(ConfirmationRecordSerializer(resolved).data)
