from __future__ import annotations

from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase

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
