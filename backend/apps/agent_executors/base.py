"""
AppExecutor — interface every app-specific executor must implement.

Executors provide optional screen hints and selector references for the LLM.
They are NOT required for execution; the LLM still decides what action to take.

Executors live in agent_executors/ only. They provide three capabilities
on top of the generic execution loop:

  1. Screen-hint inference   — classify the current screen for validation
  2. Selector registry       — versioned, ordered fallback selector lists
  3. Recovery logic          — suggest a recovery action when screen is wrong
  4. Plan validation         — app-specific static checks before approval
  5. Next-step selection     — pick the next step from the approved plan
                               (inherited from earlier iteration, kept for compat)

Naming: the class is registered to an app_package via @ExecutorRegistry.register.
Instantiated lazily — executors must be cheap to construct.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from apps.agent_core.schemas import ActionPlan, ActionStep, ScreenState


class AppExecutor(ABC):
    """
    Base class for all app-specific executors (WhatsApp, Maps, Chrome, …).

    Class attributes every subclass must define:
      app_package    – e.g. "com.whatsapp"
      supported_goals – e.g. ["send_message", "search_contact"]
    """

    app_package:     str       = ""     # must be overridden
    supported_goals: list[str] = []     # must be overridden

    # ── Screen classification ─────────────────────────────────────────────────

    @abstractmethod
    def infer_screen_hint(self, screen_state: dict) -> str:
        """
        Classify the current screen into an app-specific hint string.

        The hint is used to validate that the device is on the expected
        screen before executing each step.  Return "unknown" when classification
        is impossible; never raise.

        Common return values (app-specific, not enforced by this base):
          "chat_list", "chat_thread", "search_active", "contact_picker",
          "wrong_app", "unknown", …
        """
        raise NotImplementedError

    # ── Selector registry ─────────────────────────────────────────────────────

    @abstractmethod
    def get_selectors(
        self,
        element_name: str,
        selector_params: Optional[dict] = None,
        app_version: Optional[str] = None,
    ) -> list[dict]:
        """
        Return an ordered list of concrete selector dicts for a named element.

        Each dict maps snake_case field names to values (matching the
        Selector schema used on the Android side).

        The list is ordered by preference — callers must try them in order
        and stop at the first that matches.  If the list is empty the element
        has no registered selectors; callers should fall back to their own
        heuristics.

        selector_params: runtime values for parameterised selector templates,
            e.g. {"contact_name": "Alex"} expands '{contact_name}' placeholders.

        app_version: optional version string (e.g. "2.24.3.78") used to pick
            a version-pinned selector set when one is available.
        """
        raise NotImplementedError

    # ── Recovery logic ────────────────────────────────────────────────────────

    @abstractmethod
    def get_recovery_action(
        self,
        current_screen: str,
        expected_screen: str,
        plan_context: dict,
    ) -> Optional[dict]:
        """
        Suggest a single recovery action when the screen doesn't match expectations.

        Returns a step-params dict (same shape as ActionStep.params) if recovery
        is possible, or None if manual intervention is needed.

        plan_context: relevant plan metadata (app_package, goal, etc.).

        Example return:
          {"type": "BACK", "params": {}}
          {"type": "OPEN_APP", "params": {"package": "com.whatsapp"}}
        """
        raise NotImplementedError

    # ── Plan validation ───────────────────────────────────────────────────────

    @abstractmethod
    def validate_plan(self, plan: ActionPlan) -> list[str]:
        """
        App-specific static validation.  Returns a list of error strings.
        Called during plan compilation, before approval.  Empty list = valid.
        """
        raise NotImplementedError

    # ── Next-step selection (kept for backward compat) ────────────────────────

    def get_next_step(
        self,
        plan: ActionPlan,
        current_index: int,
        screen_state: Optional[ScreenState] = None,
    ) -> Optional[ActionStep]:
        """
        Return the step at current_index, or None when the plan is complete.
        The default implementation simply indexes into plan.steps.
        Executors may override to add app-specific skip logic.
        """
        if current_index >= len(plan.steps):
            return None
        return plan.steps[current_index]

    # ── Class-level helpers ───────────────────────────────────────────────────

    @classmethod
    def get_package(cls) -> str:
        if not cls.app_package:
            raise NotImplementedError(
                f"{cls.__name__} must define class attribute 'app_package'."
            )
        return cls.app_package


# Alias kept so existing import sites work
AbstractExecutor = AppExecutor
