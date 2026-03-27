"""Google Chrome app executor."""
from __future__ import annotations

import logging
from typing import Optional

from apps.agent_core.enums import ActionType
from apps.agent_core.schemas import ActionPlan
from apps.agent_executors.base import AppExecutor
from apps.agent_executors.registry import ExecutorRegistry

from . import selectors as _sel_module

logger = logging.getLogger(__name__)

HINT_BROWSER_OPEN   = "browser_open"
HINT_OMNIBOX_FOCUSED = "omnibox_focused"
HINT_PAGE_LOADING   = "page_loading"
HINT_PAGE_LOADED    = "page_loaded"
HINT_WRONG_APP      = "wrong_app"
HINT_UNKNOWN        = "unknown"


@ExecutorRegistry.register
class ChromeExecutor(AppExecutor):
    app_package     = "com.android.chrome"
    supported_goals = ["open_website", "search"]

    def infer_screen_hint(self, screen_state: dict) -> str:
        fg = screen_state.get("foreground_package", "")
        if fg and fg != self.app_package:
            return HINT_WRONG_APP

        nodes = screen_state.get("nodes", []) or []

        def _any(field: str, sub: str) -> bool:
            s = sub.lower()
            return any(s in (n.get(field) or "").lower() for n in nodes)

        # Omnibox has focus = typing mode
        has_focused_edit = any(
            n.get("class_name") == "android.widget.EditText" and n.get("focused")
            for n in nodes
        )
        if has_focused_edit:
            return HINT_OMNIBOX_FOCUSED

        # Page loading spinner
        if _any("content_desc", "stop loading"):
            return HINT_PAGE_LOADING

        # Standard page with address bar visible
        if (
            _any("content_desc", "search or type url")
            or _any("content_desc", "address and search bar")
        ):
            return HINT_BROWSER_OPEN

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
            logger.warning("ChromeExecutor: no selectors for '%s'", element_name)
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
        return None

    def validate_plan(self, plan: ActionPlan) -> list[str]:
        errors: list[str] = []
        if plan.app_package != self.app_package:
            errors.append(f"Plan targets '{plan.app_package}', expected '{self.app_package}'.")
        return errors
