from __future__ import annotations

from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase

from apps.agent_sessions.clarification_service import ClarificationService
from apps.agent_sessions.models import SessionStatus
from apps.audit_log.models import AuditActor, AuditEventType, AuditRecord
from apps.audit_log.services import AuditService
from apps.agent_sessions.services import SessionService


class AuditServiceTests(TestCase):
    def test_record_swallows_database_lock_errors(self):
        session = SessionService.create(
            user_id="audit-test",
            device_id="device-1",
            input_mode="text",
            reasoning_provider="openai",
            supported_packages=["com.android.chrome"],
        )

        with patch.object(
            AuditRecord,
            "save",
            side_effect=OperationalError("database is locked"),
        ):
            record = AuditService.record(
                session=session,
                event_type=AuditEventType.STEP_DISPATCHED,
                actor=AuditActor.SYSTEM,
                payload={"step": 1},
            )

        self.assertIsNone(record)
        self.assertEqual(session.audit_records.count(), 1)

    def test_clarification_audit_events_are_recorded_for_resolved_reply(self):
        session = SessionService.create(
            user_id="audit-clarification",
            device_id="device-1",
            transcript="Message Alex on WhatsApp",
            input_mode="text",
            reasoning_provider="openai",
            supported_packages=["com.whatsapp"],
        )
        session.store_intent_data(
            goal="Message Alex on WhatsApp",
            target_app="com.whatsapp",
            entities={},
        )

        ClarificationService.create_pending_query(
            session=session,
            params={
                "question": "Which Alex should I message?",
                "required_fields": ["recipient"],
                "candidates": ["Alex Chen", "Alex Johnson"],
                "reason": "multiple_visible_matches",
                "max_attempts": 3,
            },
        )
        ClarificationService.submit_reply(
            session=session,
            query_id=session.pending_user_input["query_id"],
            transcript="Alex Johnson",
        )

        event_types = list(
            AuditRecord.objects.filter(session=session).values_list("event_type", flat=True)
        )
        self.assertIn(AuditEventType.USER_INPUT_REQUESTED, event_types)
        self.assertIn(AuditEventType.USER_INPUT_RECEIVED, event_types)
        self.assertIn(AuditEventType.USER_INPUT_RESOLVED, event_types)

    def test_clarification_fallback_event_is_recorded(self):
        session = SessionService.create(
            user_id="audit-clarification-fallback",
            device_id="device-1",
            transcript="Message Alex on WhatsApp",
            input_mode="text",
            reasoning_provider="openai",
            supported_packages=["com.whatsapp"],
        )
        session.store_intent_data(
            goal="Message Alex on WhatsApp",
            target_app="com.whatsapp",
            entities={},
        )
        session.status = SessionStatus.AWAITING_USER_INPUT
        session.pending_user_input = {
            "query_id": "query_abc",
            "status": "retryable",
            "question": "Which Alex should I message?",
            "followup_question": "Which Alex should I message?",
            "attempt_count": 3,
            "max_attempts": 3,
            "required_fields": ["recipient"],
            "candidates": ["Alex Chen", "Alex Johnson"],
            "reason": "multiple_visible_matches",
            "ui_context": {"candidates": ["Alex Chen", "Alex Johnson"]},
        }
        session.save(update_fields=["status", "pending_user_input", "updated_at"])

        ClarificationService.submit_reply(
            session=session,
            query_id="query_abc",
            transcript="[silence]",
        )

        fallback = AuditRecord.objects.filter(
            session=session,
            event_type=AuditEventType.USER_INPUT_FALLBACK,
        ).last()
        self.assertIsNotNone(fallback)
        self.assertEqual(fallback.payload["why_unresolved"], "empty_reply")

    def test_clarification_followup_request_is_audited(self):
        session = SessionService.create(
            user_id="audit-clarification-followup",
            device_id="device-1",
            transcript="Message Alex on WhatsApp",
            input_mode="text",
            reasoning_provider="openai",
            supported_packages=["com.whatsapp"],
        )
        session.store_intent_data(
            goal="Message Alex on WhatsApp",
            target_app="com.whatsapp",
            entities={},
        )

        ClarificationService.create_pending_query(
            session=session,
            params={
                "question": "Which Alex should I message?",
                "required_fields": ["recipient"],
                "candidates": ["Alex Chen", "Alex Johnson"],
                "reason": "multiple_visible_matches",
                "max_attempts": 3,
            },
        )
        ClarificationService.submit_reply(
            session=session,
            query_id=session.pending_user_input["query_id"],
            transcript="Alex",
        )

        requests = list(
            AuditRecord.objects.filter(
                session=session,
                event_type=AuditEventType.USER_INPUT_REQUESTED,
            ).order_by("created_at")
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[-1].payload["attempt_count"], 2)
        self.assertEqual(requests[-1].payload["reason_unresolved"], "multiple_matches")
