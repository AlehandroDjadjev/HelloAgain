"""Brawl Stars app executor."""
from __future__ import annotations

from typing import Optional

from apps.agent_core.enums import ActionType
from apps.agent_core.schemas import ActionPlan
from apps.agent_executors.base import AppExecutor
from apps.agent_executors.registry import ExecutorRegistry

HINT_HOME = "home"
HINT_WRONG_APP = "wrong_app"
HINT_UNKNOWN = "unknown"


@ExecutorRegistry.register
class BrawlStarsExecutor(AppExecutor):
    app_package = "com.supercell.brawlstars"
    supported_goals = ["open_app"]

    def infer_screen_hint(self, screen_state: dict) -> str:
        fg = screen_state.get("foreground_package", "")
        if fg and fg != self.app_package:
            return HINT_WRONG_APP
        nodes = screen_state.get("nodes", []) or []
        if nodes:
            return HINT_HOME
        return HINT_UNKNOWN

    def get_selectors(
        self,
        element_name: str,
        selector_params: Optional[dict] = None,
        app_version: Optional[str] = None,
    ) -> list[dict]:
        return []

    def get_recovery_action(
        self,
        current_screen: str,
        expected_screen: str,
        plan_context: dict,
    ) -> Optional[dict]:
        if current_screen == HINT_WRONG_APP:
            return {
                "type": ActionType.OPEN_APP.value,
                "params": {"package": self.app_package},
            }
        return None

    def validate_plan(self, plan: ActionPlan) -> list[str]:
        errors: list[str] = []
        if plan.app_package != self.app_package:
            errors.append(
                f"Plan targets '{plan.app_package}', expected '{self.app_package}'."
            )
        return errors
