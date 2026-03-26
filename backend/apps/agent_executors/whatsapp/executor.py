"""
WhatsApp app executor.

Provides:
  - Screen-hint inference from the accessibility node tree
  - Versioned selector lookup (delegates to whatsapp.selectors)
  - Recovery action suggestions for common mismatches
  - Plan validation for WhatsApp-specific constraints
"""
from __future__ import annotations

import logging
from typing import Optional

from apps.agent_core.enums import ActionType
from apps.agent_core.schemas import ActionPlan
from apps.agent_executors.base import AppExecutor
from apps.agent_executors.registry import ExecutorRegistry

from . import selectors as _sel_module

logger = logging.getLogger(__name__)

# Screen hints produced by this executor
HINT_CHAT_LIST      = "chat_list"
HINT_CHAT_THREAD    = "chat_thread"
HINT_SEARCH_ACTIVE  = "search_active"
HINT_CONTACT_PICKER = "contact_picker"
HINT_WRONG_APP      = "wrong_app"
HINT_UNKNOWN        = "unknown"


@ExecutorRegistry.register
class WhatsAppExecutor(AppExecutor):
    app_package     = "com.whatsapp"
    supported_goals = ["send_message", "search_contact", "search", "open_chat"]

    # ── Screen-hint inference ─────────────────────────────────────────────────

    def infer_screen_hint(self, screen_state: dict) -> str:
        """
        Classify the current WhatsApp screen.

        Evaluation order (most specific first):
          1. wrong_app      — foreground package is not com.whatsapp
          2. chat_thread    — EditText (enabled) + Send button visible
          3. search_active  — focused EditText present (search is open)
          4. contact_picker — "New chat" or "Select contact" overlay
          5. chat_list      — "Search" or "New chat" button in toolbar
          6. unknown
        """
        fg = screen_state.get("foreground_package", "")
        if fg and fg != self.app_package:
            return HINT_WRONG_APP

        nodes = screen_state.get("nodes", []) or []

        def _any(field: str, substring: str) -> bool:
            """True if any node's *field* contains *substring* (case-insensitive)."""
            sub = substring.lower()
            return any(
                sub in (n.get(field) or "").lower()
                for n in nodes
            )

        def _has_class(cls: str, *, enabled: bool | None = None, focused: bool | None = None) -> bool:
            for n in nodes:
                if n.get("class_name") != cls:
                    continue
                if enabled is not None and n.get("enabled") != enabled:
                    continue
                if focused is not None and n.get("focused") != focused:
                    continue
                return True
            return False

        # 1. chat_thread: editable text field + Send action present
        has_edit  = _has_class("android.widget.EditText", enabled=True)
        has_send  = _any("content_desc", "send") or _any("text", "send")
        if has_edit and has_send:
            return HINT_CHAT_THREAD

        # 2. search_active: focused EditText (search input)
        has_focused_edit = _has_class("android.widget.EditText", focused=True)
        if has_focused_edit:
            return HINT_SEARCH_ACTIVE

        # 3. contact_picker — the screen title is "New chat" or "Select contact",
        #    distinguishing it from the chat_list which merely has a "New chat" FAB.
        title = (screen_state.get("window_title") or "").lower()
        if "new chat" in title or "select contact" in title \
                or _any("content_desc", "select contact"):
            return HINT_CONTACT_PICKER

        # 4. chat_list: main WhatsApp screen
        has_search   = _any("content_desc", "search") or _any("text", "search")
        has_new_chat = _any("content_desc", "new chat") or _any("text", "new chat")
        if has_search or has_new_chat or "whatsapp" in title:
            return HINT_CHAT_LIST

        return HINT_UNKNOWN

    # ── Selector registry ─────────────────────────────────────────────────────

    def get_selectors(
        self,
        element_name: str,
        selector_params: Optional[dict] = None,
        app_version: Optional[str] = None,
    ) -> list[dict]:
        """Delegate to the versioned selector module."""
        result = _sel_module.get_selectors(
            element_name,
            selector_params=selector_params,
            app_version=app_version,
        )
        if not result:
            logger.warning(
                "WhatsAppExecutor: no selectors registered for element '%s'",
                element_name,
            )
        return result

    # ── Recovery logic ────────────────────────────────────────────────────────

    def get_recovery_action(
        self,
        current_screen: str,
        expected_screen: str,
        plan_context: dict,
    ) -> Optional[dict]:
        """
        Suggest one recovery action for a screen mismatch.

        Logic table:
          expected=chat_list,   current=chat_thread  → BACK
          expected=chat_list,   current=search_active→ BACK
          expected=chat_thread, current=chat_list    → None (re-search needed)
          expected=*,           current=wrong_app    → OPEN_APP com.whatsapp
          expected=*,           current=unknown      → BACK (try to return to known state)
          any other combination                       → None (manual takeover)
        """
        if current_screen == HINT_WRONG_APP:
            logger.info("Recovery: wrong app in foreground → relaunch WhatsApp")
            return {
                "type": ActionType.OPEN_APP.value,
                "params": {"package": self.app_package},
            }

        if current_screen == HINT_UNKNOWN:
            logger.info("Recovery: unknown screen → BACK")
            return {"type": ActionType.BACK.value, "params": {}}

        if expected_screen == HINT_CHAT_LIST and current_screen in (
            HINT_CHAT_THREAD, HINT_SEARCH_ACTIVE, HINT_CONTACT_PICKER
        ):
            logger.info(
                "Recovery: expected chat_list, got %s → BACK", current_screen
            )
            return {"type": ActionType.BACK.value, "params": {}}

        if expected_screen == HINT_SEARCH_ACTIVE and current_screen == HINT_CHAT_LIST:
            # Need to re-tap the search button
            search_candidates = self.get_selectors("search_button")
            if search_candidates:
                return {
                    "type": ActionType.TAP_ELEMENT.value,
                    "params": {"selector_candidates": search_candidates},
                }

        # chat_thread expected but got chat_list → need to find the contact again
        if expected_screen == HINT_CHAT_THREAD and current_screen == HINT_CHAT_LIST:
            return None  # execution service should back off and re-search

        return None

    # ── Plan validation ───────────────────────────────────────────────────────

    def validate_plan(self, plan: ActionPlan) -> list[str]:
        errors: list[str] = []

        if plan.app_package != self.app_package:
            errors.append(
                f"Plan targets '{plan.app_package}', "
                f"expected '{self.app_package}'."
            )

        # Every TYPE_TEXT must be preceded by a TAP/FOCUS on a relevant element
        for i, step in enumerate(plan.steps):
            if step.type == ActionType.TYPE_TEXT:
                if i == 0:
                    errors.append(
                        f"Step '{step.id}' (TYPE_TEXT) is the first step — "
                        "no preceding focus action."
                    )
                else:
                    prev = plan.steps[i - 1]
                    if prev.type not in (
                        ActionType.TAP_ELEMENT,
                        ActionType.FOCUS_ELEMENT,
                        ActionType.LONG_PRESS_ELEMENT,
                    ):
                        errors.append(
                            f"Step '{step.id}' (TYPE_TEXT) is not preceded by "
                            f"a tap/focus action (got '{prev.type.value}')."
                        )

        return errors
