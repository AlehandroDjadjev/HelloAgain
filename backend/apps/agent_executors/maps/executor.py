"""Google Maps app executor."""
from __future__ import annotations

import logging
from typing import Optional

from apps.agent_core.enums import ActionType
from apps.agent_core.schemas import ActionPlan
from apps.agent_executors.base import AppExecutor
from apps.agent_executors.registry import ExecutorRegistry

from . import selectors as _sel_module

logger = logging.getLogger(__name__)

HINT_MAP_VIEW          = "map_view"
HINT_SEARCH_ACTIVE     = "search_active"
HINT_ROUTE_PREVIEW     = "route_preview"
HINT_NAVIGATION_ACTIVE = "navigation_active"
HINT_WRONG_APP         = "wrong_app"
HINT_UNKNOWN           = "unknown"


@ExecutorRegistry.register
class MapsExecutor(AppExecutor):
    app_package     = "com.google.android.apps.maps"
    supported_goals = ["navigate_to", "start_navigation", "search"]

    def infer_screen_hint(self, screen_state: dict) -> str:
        fg = screen_state.get("foreground_package", "")
        if fg and fg != self.app_package:
            return HINT_WRONG_APP

        nodes = screen_state.get("nodes", []) or []

        def _any(field: str, sub: str) -> bool:
            s = sub.lower()
            return any(s in (n.get(field) or "").lower() for n in nodes)

        # Navigation active: "End route" or "Overview" visible
        if _any("text", "end route") or _any("content_desc", "end route") \
                or _any("content_desc", "exit navigation"):
            return HINT_NAVIGATION_ACTIVE

        # Route preview: "Start" and "Directions" visible
        if _any("content_desc", "start") and _any("content_desc", "directions"):
            return HINT_ROUTE_PREVIEW

        # Search active: EditText focused
        has_focused = any(
            n.get("class_name") == "android.widget.EditText" and n.get("focused")
            for n in nodes
        )
        if has_focused:
            return HINT_SEARCH_ACTIVE

        # Map view: search bar and map elements present
        if _any("content_desc", "search") or _any("content_desc", "your location"):
            return HINT_MAP_VIEW

        return HINT_UNKNOWN

    def get_selectors(
        self,
        element_name: str,
        selector_params: Optional[dict] = None,
        app_version: Optional[str] = None,
    ) -> list[dict]:
        result = _sel_module.get_selectors(
            element_name, selector_params=selector_params, app_version=app_version
        )
        if not result:
            logger.warning("MapsExecutor: no selectors for '%s'", element_name)
        return result

    def get_recovery_action(
        self,
        current_screen: str,
        expected_screen: str,
        plan_context: dict,
    ) -> Optional[dict]:
        if current_screen == HINT_WRONG_APP:
            return {"type": ActionType.OPEN_APP.value,
                    "params": {"package": self.app_package}}
        if current_screen == HINT_UNKNOWN:
            return {"type": ActionType.BACK.value, "params": {}}
        if expected_screen == HINT_MAP_VIEW and current_screen in (
            HINT_ROUTE_PREVIEW, HINT_SEARCH_ACTIVE, HINT_NAVIGATION_ACTIVE
        ):
            return {"type": ActionType.BACK.value, "params": {}}
        return None

    def validate_plan(self, plan: ActionPlan) -> list[str]:
        errors: list[str] = []
        if plan.app_package != self.app_package:
            errors.append(f"Plan targets '{plan.app_package}', expected '{self.app_package}'.")
        return errors
