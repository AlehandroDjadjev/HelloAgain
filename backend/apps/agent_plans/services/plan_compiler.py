"""
PlanCompiler — deterministic, template-based plan compilation.

The LLM is NOT used here. Templates are pure functions that take
IntentResult entities and return a list of ActionStep objects.

ADDING A NEW TEMPLATE:
  1. Write a function with signature:
       def _my_template(entities: dict) -> list[ActionStep]:
  2. Register it:
       _REGISTRY[("goal_type", "com.example.app")] = _my_template
     Or use a wildcard app:
       _REGISTRY[("open_app", "*")] = _open_app_any

All templates must:
  - Include REQUEST_CONFIRMATION immediately before any step with
    requires_confirmation=True  (enforced by Pydantic ActionPlan validator)
  - Not exceed 30 steps  (PolicyConfig.max_steps_per_plan default)
"""
from __future__ import annotations

import logging
import uuid
from typing import Callable

from apps.agent_core.enums import ActionSensitivity, ActionType
from apps.agent_core.schemas import ActionPlan, ActionStep, ExpectedOutcome, RetryPolicy

from .intent_service import IntentResult

logger = logging.getLogger(__name__)

# ── Type alias ────────────────────────────────────────────────────────────────

TemplateFunc = Callable[[dict], list[ActionStep]]

# ── Template registry ─────────────────────────────────────────────────────────

_REGISTRY: dict[tuple[str, str], TemplateFunc] = {}


def register_template(goal_type: str, app_package: str) -> Callable[[TemplateFunc], TemplateFunc]:
    """Decorator: @register_template("send_message", "com.whatsapp")"""
    def decorator(fn: TemplateFunc) -> TemplateFunc:
        _REGISTRY[(goal_type, app_package)] = fn
        return fn
    return decorator


def _lookup(goal_type: str, app_package: str) -> TemplateFunc | None:
    """Exact match first, then wildcard app."""
    return _REGISTRY.get((goal_type, app_package)) or _REGISTRY.get((goal_type, "*"))


# ── PlanCompiler ──────────────────────────────────────────────────────────────

class PlanCompiler:
    """
    Compile a deterministic ActionPlan from an IntentResult.
    Raises CompilationError if no template is found for the (goal_type, app_package).
    """

    @staticmethod
    def compile(intent: IntentResult, session_id: str) -> ActionPlan:
        template_fn = _lookup(intent.goal_type, intent.app_package)
        if template_fn is None:
            raise CompilationError(
                f"No template for goal_type={intent.goal_type!r} "
                f"app_package={intent.app_package!r}. "
                f"Register a template function in plan_compiler.py."
            )

        steps = template_fn(intent.entities)
        plan_id = uuid.uuid4().hex

        plan = ActionPlan(
            plan_id=plan_id,
            session_id=session_id,
            goal=intent.goal[:500],
            app_package=intent.app_package,
            steps=steps,
            version=1,
        )
        logger.info(
            "Compiled plan %s: %d steps for %s (%s)",
            plan_id, len(steps), intent.goal_type, intent.app_package,
        )
        return plan

    @staticmethod
    def has_template(goal_type: str, app_package: str) -> bool:
        return _lookup(goal_type, app_package) is not None


class CompilationError(Exception):
    pass


# ── Step factory helpers ───────────────────────────────────────────────────────

def _step(
    sid: str,
    action_type: ActionType,
    params: dict,
    hint: str = "",
    sensitivity: ActionSensitivity = ActionSensitivity.LOW,
    requires_confirmation: bool = False,
    timeout_ms: int = 5000,
    max_attempts: int = 2,
) -> ActionStep:
    outcome = ExpectedOutcome(screen_hint=hint) if hint else None
    return ActionStep(
        id=sid,
        type=action_type,
        params=params,
        expected_outcome=outcome,
        timeout_ms=timeout_ms,
        retry_policy=RetryPolicy(max_attempts=max_attempts),
        sensitivity=sensitivity,
        requires_confirmation=requires_confirmation,
    )


def _sel(**kwargs) -> dict:
    """Build a concrete selector dict, omitting None values."""
    return {k: v for k, v in kwargs.items() if v is not None}


def _named(name: str, **selector_params) -> dict:
    """
    Named selector reference — resolved to concrete selector candidates by the
    executor registry at execution time.

    Use this instead of _sel() for elements that have a versioned selector
    entry in the app executor's selector registry.  selector_params are
    forwarded for runtime placeholder substitution (e.g. contact_name).
    """
    result: dict = {"selector_name": name}
    if selector_params:
        result["selector_params"] = selector_params
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Templates
# ══════════════════════════════════════════════════════════════════════════════

# ── WhatsApp: send_message ────────────────────────────────────────────────────

@register_template("send_message", "com.whatsapp")
def _whatsapp_send_message(entities: dict) -> list[ActionStep]:
    recipient = entities.get("recipient", "contact")
    message   = entities.get("message", entities.get("text", ""))
    preview   = message[:80] + ("…" if len(message) > 80 else "")

    return [
        _step("wa_1", ActionType.OPEN_APP,
              {"package": "com.whatsapp"},
              hint="chat_list", timeout_ms=8000),

        _step("wa_2", ActionType.WAIT_FOR_APP,
              {"package": "com.whatsapp", "timeout_ms": 6000},
              hint="chat_list", timeout_ms=6000),

        _step("wa_3", ActionType.ASSERT_SCREEN,
              {"screen_hint": "chat_list"},
              hint="chat_list"),

        # Uses named selector → resolved at execution time via WhatsAppExecutor
        _step("wa_4", ActionType.TAP_ELEMENT,
              _named("search_button"),
              hint="search_active"),

        # Search input gains focus — tap it to ensure keyboard opens
        _step("wa_5", ActionType.TAP_ELEMENT,
              _named("search_input"),
              hint="search_active", max_attempts=1, timeout_ms=3000),

        _step("wa_6", ActionType.TYPE_TEXT,
              {"text": recipient},
              hint="search_active"),

        # Parameterised selector: {contact_name} substituted with recipient at runtime
        _step("wa_7", ActionType.TAP_ELEMENT,
              _named("contact_item", contact_name=recipient),
              hint="chat_thread", max_attempts=3),

        # Tap message input box
        _step("wa_8", ActionType.TAP_ELEMENT,
              _named("message_input"),
              hint="chat_thread",
              sensitivity=ActionSensitivity.MEDIUM),

        _step("wa_9", ActionType.TYPE_TEXT,
              {"text": message},
              hint="chat_thread",
              sensitivity=ActionSensitivity.MEDIUM),

        # Confirmation gate — must immediately precede the requires_confirmation step
        _step("wa_10", ActionType.REQUEST_CONFIRMATION,
              {
                  "action_summary": f"Send WhatsApp to {recipient}: \"{preview}\"",
                  "recipient":      recipient,
                  "content_preview": message[:200],
              },
              hint="confirmation_shown",
              sensitivity=ActionSensitivity.MEDIUM),

        _step("wa_11", ActionType.TAP_ELEMENT,
              _named("send_button"),
              hint="message_sent",
              sensitivity=ActionSensitivity.HIGH,
              requires_confirmation=True),
    ]


# ── WhatsApp: search ──────────────────────────────────────────────────────────

@register_template("search", "com.whatsapp")
@register_template("search_contact", "com.whatsapp")
def _whatsapp_search(entities: dict) -> list[ActionStep]:
    query = entities.get("query", entities.get("recipient", ""))

    return [
        _step("wa_1", ActionType.OPEN_APP,
              {"package": "com.whatsapp"},
              hint="chat_list", timeout_ms=8000),

        _step("wa_2", ActionType.WAIT_FOR_APP,
              {"package": "com.whatsapp", "timeout_ms": 6000},
              hint="chat_list"),

        _step("wa_3", ActionType.TAP_ELEMENT,
              _named("search_button"),
              hint="search_active"),

        _step("wa_4", ActionType.TAP_ELEMENT,
              _named("search_input"),
              hint="search_active", max_attempts=1, timeout_ms=3000),

        _step("wa_5", ActionType.TYPE_TEXT,
              {"text": query},
              hint="search_active"),
    ]


# ── Google Maps: navigate_to / start_navigation ───────────────────────────────

@register_template("navigate_to", "com.google.android.apps.maps")
@register_template("start_navigation", "com.google.android.apps.maps")
def _maps_navigate(entities: dict) -> list[ActionStep]:
    destination = entities.get("destination", "")

    return [
        _step("mp_1", ActionType.OPEN_APP,
              {"package": "com.google.android.apps.maps"},
              hint="map_view", timeout_ms=8000),

        _step("mp_2", ActionType.WAIT_FOR_APP,
              {"package": "com.google.android.apps.maps", "timeout_ms": 6000},
              hint="map_view"),

        _step("mp_3", ActionType.TAP_ELEMENT,
              {"selector": _sel(content_desc="Search here")},
              hint="search_open"),

        _step("mp_4", ActionType.TYPE_TEXT,
              {"text": destination},
              hint="search_results"),

        _step("mp_5", ActionType.TAP_ELEMENT,
              {"selector": _sel(text=destination)},
              hint="destination_selected", max_attempts=3),

        _step("mp_6", ActionType.TAP_ELEMENT,
              {"selector": _sel(content_desc="Directions")},
              hint="directions_ready"),

        _step("mp_7", ActionType.TAP_ELEMENT,
              {"selector": _sel(content_desc="Start")},
              hint="navigation_started",
              sensitivity=ActionSensitivity.MEDIUM),
    ]


# ── Chrome: open_website ──────────────────────────────────────────────────────

@register_template("open_website", "com.android.chrome")
def _chrome_open_website(entities: dict) -> list[ActionStep]:
    url = entities.get("url", entities.get("query", ""))

    return [
        _step("ch_1", ActionType.OPEN_APP,
              {"package": "com.android.chrome"},
              hint="browser_open", timeout_ms=8000),

        _step("ch_2", ActionType.WAIT_FOR_APP,
              {"package": "com.android.chrome", "timeout_ms": 6000},
              hint="browser_open"),

        _step("ch_3", ActionType.TAP_ELEMENT,
              {"selector": _sel(content_desc="Search or type URL")},
              hint="omnibox_focused"),

        _step("ch_4", ActionType.TYPE_TEXT,
              {"text": url},
              hint="url_typed"),

        _step("ch_5", ActionType.TAP_ELEMENT,
              {"selector": _sel(content_desc="Go", class_name="android.widget.ImageView")},
              hint="page_loading"),
    ]


# ── Chrome: search ────────────────────────────────────────────────────────────

@register_template("search", "com.android.chrome")
def _chrome_search(entities: dict) -> list[ActionStep]:
    query = entities.get("query", "")

    return [
        _step("ch_1", ActionType.OPEN_APP,
              {"package": "com.android.chrome"},
              hint="browser_open", timeout_ms=8000),

        _step("ch_2", ActionType.WAIT_FOR_APP,
              {"package": "com.android.chrome", "timeout_ms": 6000},
              hint="browser_open"),

        _step("ch_3", ActionType.TAP_ELEMENT,
              {"selector": _sel(content_desc="Search or type URL")},
              hint="omnibox_focused"),

        _step("ch_4", ActionType.TYPE_TEXT,
              {"text": query},
              hint="query_typed"),

        _step("ch_5", ActionType.TAP_ELEMENT,
              {"selector": _sel(content_desc="Go", class_name="android.widget.ImageView")},
              hint="results_loading"),
    ]


# ── Gmail: draft_email ────────────────────────────────────────────────────────

@register_template("draft_email", "com.google.android.gm")
def _gmail_draft_email(entities: dict) -> list[ActionStep]:
    to = entities.get("recipient", entities.get("to", "recipient@example.com"))
    subject = entities.get("subject", "(no subject)")
    body = entities.get("body", entities.get("message", ""))
    preview = body[:80] + ("…" if len(body) > 80 else "")

    return [
        _step("gm_1", ActionType.OPEN_APP,
              {"package": "com.google.android.gm"},
              hint="inbox", timeout_ms=8000),

        _step("gm_2", ActionType.WAIT_FOR_APP,
              {"package": "com.google.android.gm", "timeout_ms": 6000},
              hint="inbox"),

        _step("gm_3", ActionType.TAP_ELEMENT,
              {"selector": _sel(content_desc="Compose")},
              hint="compose_open"),

        _step("gm_4", ActionType.TAP_ELEMENT,
              {"selector": _sel(content_desc="To")},
              hint="to_focused"),

        _step("gm_5", ActionType.TYPE_TEXT,
              {"text": to},
              hint="to_filled"),

        _step("gm_6", ActionType.TAP_ELEMENT,
              {"selector": _sel(content_desc="Subject")},
              hint="subject_focused"),

        _step("gm_7", ActionType.TYPE_TEXT,
              {"text": subject},
              hint="subject_filled"),

        _step("gm_8", ActionType.TAP_ELEMENT,
              {"selector": _sel(content_desc="Compose email")},
              hint="body_focused"),

        _step("gm_9", ActionType.TYPE_TEXT,
              {"text": body},
              hint="body_filled",
              sensitivity=ActionSensitivity.MEDIUM),

        _step("gm_10", ActionType.REQUEST_CONFIRMATION,
              {
                  "action_summary": f"Send email to {to}: \"{subject}\" — {preview}",
                  "recipient": to,
                  "content_preview": body[:200],
              },
              hint="confirmation_shown",
              sensitivity=ActionSensitivity.MEDIUM),

        _step("gm_11", ActionType.TAP_ELEMENT,
              {"selector": _sel(content_desc="Send")},
              hint="email_sent",
              sensitivity=ActionSensitivity.HIGH,
              requires_confirmation=True),
    ]


# ── Generic: open_app (wildcard) ──────────────────────────────────────────────

@register_template("open_app", "*")
def _open_app_any(entities: dict) -> list[ActionStep]:
    package = entities.get("package", entities.get("app_package", ""))

    return [
        _step("oa_1", ActionType.OPEN_APP,
              {"package": package},
              hint="app_open", timeout_ms=8000),

        _step("oa_2", ActionType.WAIT_FOR_APP,
              {"package": package, "timeout_ms": 6000},
              hint="app_open"),

        _step("oa_3", ActionType.ASSERT_SCREEN,
              {"screen_hint": "app_open", "required_package": package},
              hint="app_open"),
    ]
