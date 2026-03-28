from __future__ import annotations

from unittest.mock import Mock, patch

from django.db import OperationalError
from django.test import SimpleTestCase, TestCase

from apps.agent_plans.models import IntentRecord
from apps.agent_plans.services.intent_service import IntentService
from apps.agent_plans.services.plan_compiler import PlanCompiler
from apps.agent_plans.services.plan_service import PlanService
from apps.agent_sessions.services import SessionService


class BrawlStarsSupportTests(SimpleTestCase):
    def test_keyword_fallback_detects_brawl_stars_open_app(self):
        result = IntentService()._fallback_service.parse("Open Brawl Stars")

        self.assertEqual(result.app_package, "com.supercell.brawlstars")
        self.assertEqual(result.target_app, "Brawl Stars")
        self.assertEqual(result.goal_type, "open_app")

    def test_plan_compiler_has_open_app_template_for_brawl_stars(self):
        self.assertTrue(
            PlanCompiler.has_template("open_app", "com.supercell.brawlstars")
        )

    def test_plan_service_returns_generic_executor_hint_for_brawl_stars(self):
        self.assertEqual(
            PlanService.get_executor_hint("com.supercell.brawlstars"),
            "generic_v1",
        )

    def test_parse_intent_falls_back_when_llm_returns_blank_target_app(self):
        client = Mock()
        client.generate.return_value = {
            "goal": "Search Jeffrey Epstein on Chrome",
            "goal_type": "search",
            "target_app": "",
            "entities": {"query": "Jeffrey Epstein"},
            "risk_level": "medium",
            "confidence": 0.8,
            "ambiguity_flags": [],
        }

        result = IntentService(client=client).parse_intent(
            "Search up Jeffrey Epstien on Chrome"
        )

        self.assertEqual(result.app_package, "com.android.chrome")
        self.assertEqual(result.goal_type, "search")
        self.assertIn("keyword detection fallback", " ".join(result.ambiguity_flags))

    def test_parse_intent_accepts_dynamic_supported_package(self):
        client = Mock()
        client.generate.return_value = {
            "goal": "Open Instagram",
            "goal_type": "open_app",
            "target_app": "com.instagram.android",
            "entities": {},
            "risk_level": "low",
            "confidence": 0.93,
            "ambiguity_flags": [],
        }

        result = IntentService(client=client).parse_intent(
            "Open Instagram",
            supported_packages=["com.instagram.android", "com.android.chrome"],
        )

        self.assertEqual(result.app_package, "com.instagram.android")
        self.assertEqual(result.goal_type, "open_app")

    def test_parse_intent_keeps_invalid_request_from_llm(self):
        client = Mock()
        client.generate.return_value = {
            "goal": "No actionable phone command",
            "goal_type": "invalid_request",
            "target_app": "",
            "entities": {},
            "risk_level": "low",
            "confidence": 0.15,
            "ambiguity_flags": ["not_actionable_request"],
        }

        result = IntentService(client=client).parse_intent("How are you today?")

        self.assertEqual(result.goal_type, "invalid_request")
        self.assertEqual(result.app_package, "")
        self.assertIn("not_actionable_request", result.ambiguity_flags)
        self.assertLess(result.confidence, 0.5)

    def test_keyword_fallback_marks_unknown_request_as_invalid(self):
        result = IntentService()._fallback_service.parse("Tell me a joke")

        self.assertEqual(result.goal_type, "invalid_request")
        self.assertEqual(result.app_package, "")
        self.assertIn("not_actionable_request", result.ambiguity_flags)
        self.assertLess(result.confidence, 0.5)

    def test_keyword_fallback_detects_take_me_to_as_navigation(self):
        result = IntentService()._fallback_service.parse("Take me to Central Park")

        self.assertEqual(result.goal_type, "navigate_to")
        self.assertEqual(result.app_package, "com.google.android.apps.maps")
        self.assertEqual(result.entities.get("destination"), "central park")


class PlanServiceStoreIntentTests(TestCase):
    def test_store_intent_retries_after_sqlite_lock(self):
        session = SessionService.create(
            user_id="plan-test",
            device_id="device-1",
            transcript="open chrome",
            input_mode="text",
            supported_packages=["com.android.chrome"],
        )

        real_create = IntentRecord.objects.create
        calls = {"count": 0}

        def flaky_create(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OperationalError("database is locked")
            return real_create(*args, **kwargs)

        with patch.object(IntentRecord.objects, "create", side_effect=flaky_create):
            intent = PlanService.store_intent(
                session=session,
                raw_transcript="open chrome",
                parsed_intent={"app_package": "com.android.chrome"},
                goal_type="open_app",
                confidence=0.8,
            )

        self.assertEqual(intent.session_id, session.id)
        self.assertEqual(IntentRecord.objects.filter(session=session).count(), 1)

    def test_store_intent_updates_existing_record(self):
        session = SessionService.create(
            user_id="plan-test",
            device_id="device-2",
            transcript="first",
            input_mode="text",
            supported_packages=["com.android.chrome"],
        )

        first = PlanService.store_intent(
            session=session,
            raw_transcript="first",
            parsed_intent={"app_package": "com.android.chrome"},
            goal_type="open_app",
            confidence=0.6,
        )
        second = PlanService.store_intent(
            session=session,
            raw_transcript="second",
            parsed_intent={"app_package": "com.whatsapp"},
            goal_type="send_message",
            confidence=0.9,
        )

        self.assertEqual(first.id, second.id)
        second.refresh_from_db()
        self.assertEqual(second.raw_transcript, "second")
        self.assertEqual(second.goal_type, "send_message")
