from datetime import datetime, timezone

from django.test import TestCase

from apps.agent_sessions.services import SessionService

from .services import DeviceBridgeService


class DeviceBridgeServiceTests(TestCase):
    def test_ingest_screen_state_allows_null_focused_element_ref(self):
        session = SessionService.create(
            user_id="test_user",
            device_id="test_device",
            input_mode="text",
            supported_packages=["com.android.chrome"],
        )

        state = DeviceBridgeService.ingest_screen_state(
            session=session,
            step_id="llm_step_1",
            foreground_package="com.android.chrome",
            window_title="Chrome",
            screen_hash="abc123",
            is_sensitive=False,
            nodes=[],
            captured_at=datetime.now(timezone.utc),
            focused_element_ref=None,
        )

        self.assertEqual(state.focused_element_ref, "")
