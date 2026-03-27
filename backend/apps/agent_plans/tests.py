from __future__ import annotations

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
