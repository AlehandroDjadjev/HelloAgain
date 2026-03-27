from __future__ import annotations

from unittest.mock import Mock

from django.test import SimpleTestCase

from apps.agent_plans.services.intent_service import IntentService
from apps.agent_plans.services.plan_compiler import PlanCompiler
from apps.agent_plans.services.plan_service import PlanService


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
