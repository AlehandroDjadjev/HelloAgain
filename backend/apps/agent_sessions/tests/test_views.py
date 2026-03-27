from __future__ import annotations

from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase

from apps.agent_sessions.services import SessionService
from apps.agent_sessions.views import _get_session


class SessionViewHelpersTests(TestCase):
    def test_get_session_retries_after_sqlite_lock(self):
        session = SessionService.create(
            user_id="view-test",
            device_id="device-1",
            input_mode="text",
            supported_packages=["com.android.chrome"],
        )

        with patch(
            "apps.agent_sessions.views.AgentSession.objects.get",
            side_effect=[
                OperationalError("database is locked"),
                session,
            ],
        ) as mocked_get:
            resolved = _get_session(session.id)

        self.assertEqual(resolved.id, session.id)
        self.assertEqual(mocked_get.call_count, 2)
