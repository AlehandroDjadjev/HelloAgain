"""
Typed Python implementation of the HelloAgain execution contracts.

These models mirror shared/action_schemas/*.schema.json exactly.
Pydantic v2 is the source of validation truth on the backend;
JSON Schema files are the canonical wire-format for Android/Flutter.

Rule: natural language must never reach or pass through any of these types.
The 'goal' field in ActionPlan is the only place a human-readable string
is permitted, and it is bounded to a short structured description.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from .enums import (
    ActionErrorCode,
    ActionResultStatus,
    ActionSensitivity,
    ActionType,
    ConfirmationStatus,
    RiskLevel,
    ScrollDirection,
    SensitiveScreenPolicy,
)


def _new_id() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Primitive sub-objects ────────────────────────────────────────────────────

class Bounds(BaseModel):
    left: int
    top: int
    right: int
    bottom: int

    model_config = {"frozen": True}


class Selector(BaseModel):
    """
    Identifies a UI node in the Android accessibility tree.

    Resolution priority (highest → lowest):
        element_ref → view_id → content_desc → text
        → class_name + index_in_parent → bounds (last resort only).

    Bounds-only selectors are rejected unless
    PolicyConfig.allow_coordinates_fallback is True.
    """

    element_ref: Optional[str] = None
    view_id: Optional[str] = None
    content_desc: Optional[str] = None
    text: Optional[str] = None
    class_name: Optional[str] = None
    index_in_parent: Optional[int] = Field(default=None, ge=0)
    bounds: Optional[Bounds] = None

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> "Selector":
        fields = [
            self.element_ref,
            self.view_id,
            self.content_desc,
            self.text,
            self.class_name,
            self.bounds,
        ]
        if not any(f is not None for f in fields):
            raise ValueError("Selector must specify at least one identifying field.")
        return self

    model_config = {"frozen": True}


# ── ScreenState ──────────────────────────────────────────────────────────────

class AccessibilityNode(BaseModel):
    ref: str
    text: Optional[str] = None
    content_desc: Optional[str] = None
    view_id: Optional[str] = None
    class_name: Optional[str] = None
    package_name: Optional[str] = None
    parent_ref: Optional[str] = None
    bounds: Optional[Bounds] = None
    clickable: bool = False
    long_clickable: bool = False
    scrollable: bool = False
    enabled: bool = True
    focused: bool = False
    selected: bool = False
    editable: bool = False
    checkable: bool = False
    checked: bool = False
    index_in_parent: int = Field(default=0, ge=0)
    child_count: int = Field(default=0, ge=0)
    children: List[str] = Field(default_factory=list)

    model_config = {"frozen": True}


class ScreenState(BaseModel):
    """
    Structured snapshot returned by AccessibilityService after every action.
    Must never contain raw screenshots.
    If is_sensitive is True, the executor must abort or pause immediately.
    """

    foreground_package: str
    window_title: Optional[str] = None
    screen_hash: str
    focused_element_ref: Optional[str] = None
    is_sensitive: bool = False
    nodes: List[AccessibilityNode] = Field(default_factory=list)
    captured_at: datetime = Field(default_factory=_utcnow)


# ── Action-specific parameter models ─────────────────────────────────────────

class OpenAppParams(BaseModel):
    package: str


class WaitForAppParams(BaseModel):
    package: str
    timeout_ms: int = Field(default=5000, ge=0)


class WaitForElementParams(BaseModel):
    selector: Selector
    timeout_ms: int = Field(default=5000, ge=0)


class FindElementParams(BaseModel):
    selector: Selector


class TapElementParams(BaseModel):
    selector: Selector


class LongPressElementParams(BaseModel):
    selector: Selector
    duration_ms: int = Field(default=1000, ge=0)


class FocusElementParams(BaseModel):
    selector: Selector


class TypeTextParams(BaseModel):
    text: str = Field(min_length=1)
    selector: Optional[Selector] = None
    append: bool = False


class ClearTextParams(BaseModel):
    selector: Optional[Selector] = None


class ScrollParams(BaseModel):
    direction: ScrollDirection
    selector: Optional[Selector] = None
    distance_dp: Optional[int] = Field(default=None, ge=1)
    to_end: bool = False


class SwipeParams(BaseModel):
    start_x: int
    start_y: int
    end_x: int
    end_y: int
    duration_ms: int = Field(default=300, ge=0)


class AssertScreenParams(BaseModel):
    screen_hint: str
    required_package: Optional[str] = None


class AssertElementParams(BaseModel):
    selector: Selector
    assert_visible: bool = True
    assert_enabled: Optional[bool] = None
    assert_text: Optional[str] = None


class RequestConfirmationParams(BaseModel):
    action_summary: str
    recipient: Optional[str] = None
    content_preview: Optional[str] = None


class AbortParams(BaseModel):
    reason: str


# ── ActionStep building blocks ───────────────────────────────────────────────

class RetryPolicy(BaseModel):
    max_attempts: int = Field(default=2, ge=1, le=5)
    backoff_ms: int = Field(default=500, ge=0)

    model_config = {"frozen": True}


class ExpectedOutcome(BaseModel):
    screen_hint: Optional[str] = None
    element_visible: Optional[str] = None
    element_gone: Optional[str] = None
    package: Optional[str] = None

    @model_validator(mode="after")
    def require_at_least_one(self) -> "ExpectedOutcome":
        if not any(
            [self.screen_hint, self.element_visible, self.element_gone, self.package]
        ):
            raise ValueError("ExpectedOutcome must specify at least one condition.")
        return self

    model_config = {"frozen": True}


# ── ActionStep ───────────────────────────────────────────────────────────────

class ActionStep(BaseModel):
    """
    A single typed, fully-parameterised execution step.
    Executors process one step at a time and must verify expected_outcome
    before advancing. Batching UI actions is forbidden.
    """

    id: str = Field(default_factory=lambda: f"step_{_new_id()[:8]}")
    type: ActionType
    params: Dict[str, Any] = Field(default_factory=dict)
    expected_outcome: Optional[ExpectedOutcome] = None
    timeout_ms: int = Field(default=5000, ge=0)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    sensitivity: ActionSensitivity = ActionSensitivity.LOW
    requires_confirmation: bool = False


# ── ActionPlan ───────────────────────────────────────────────────────────────

class ActionPlan(BaseModel):
    """
    Immutable, policy-approved plan for a single user goal.
    Once approved, no component may add, remove, or reorder steps.
    Natural language transcripts must never appear in this model.
    """

    plan_id: str = Field(default_factory=_new_id)
    session_id: str
    goal: str = Field(
        max_length=500,
        description=(
            "Short structured description of the goal — NOT a raw transcript. "
            "Example: 'Send WhatsApp message to +1-555-0100: meeting at 3pm'"
        ),
    )
    app_package: str
    steps: List[ActionStep] = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def validate_confirmation_ordering(self) -> "ActionPlan":
        """
        Every step with requires_confirmation=True must be immediately
        preceded by a REQUEST_CONFIRMATION step.
        """
        for i, step in enumerate(self.steps):
            if step.requires_confirmation:
                if i == 0 or self.steps[i - 1].type != ActionType.REQUEST_CONFIRMATION:
                    raise ValueError(
                        f"Step '{step.id}' (type={step.type}) has requires_confirmation=True "
                        f"but is not immediately preceded by a REQUEST_CONFIRMATION step."
                    )
        return self


# ── ActionResult ─────────────────────────────────────────────────────────────

class ActionResult(BaseModel):
    """
    Outcome posted by Android after executing one ActionStep.
    'success' means expected_outcome was verified.
    Any other status halts normal execution flow.
    """

    step_id: str
    session_id: str
    plan_id: str
    status: ActionResultStatus
    screen_state: Optional[ScreenState] = None
    error_code: Optional[ActionErrorCode] = None
    error_detail: Optional[str] = None
    executed_at: datetime = Field(default_factory=_utcnow)
    duration_ms: int = Field(default=0, ge=0)


# ── ConfirmationRequest ───────────────────────────────────────────────────────

class ConfirmationRequest(BaseModel):
    """
    Displayed to the user before any irreversible action executes.
    The UI must render all non-null fields.
    Execution only resumes when status transitions to 'approved'.
    Silent auto-approval is never permitted.
    """

    confirmation_id: str = Field(default_factory=_new_id)
    session_id: str
    plan_id: str
    step_id: str
    app_name: str
    app_package: str
    action_summary: str
    recipient: Optional[str] = None
    content_preview: Optional[str] = None
    sensitivity: ActionSensitivity
    status: ConfirmationStatus = ConfirmationStatus.PENDING
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None


# ── PolicyConfig ─────────────────────────────────────────────────────────────

class PolicyConfig(BaseModel):
    """
    Safety and permission rules evaluated before plan approval and at each step.
    Derived by merging user-level UserAutomationPolicy with org-level defaults.
    No component may bypass these rules.
    """

    user_id: Optional[str] = None
    org_id: Optional[str] = None
    allowed_packages: List[str] = Field(default_factory=lambda: ["com.whatsapp"])
    blocked_action_types: List[ActionType] = Field(default_factory=list)
    always_confirm_action_types: List[ActionType] = Field(
        default_factory=lambda: [ActionType.REQUEST_CONFIRMATION]
    )
    max_steps_per_plan: int = Field(default=30, ge=1, le=100)
    sensitive_screen_policy: SensitiveScreenPolicy = SensitiveScreenPolicy.ABORT
    risk_threshold: RiskLevel = RiskLevel.MEDIUM
    allow_coordinates_fallback: bool = False

    @model_validator(mode="after")
    def packages_not_empty(self) -> "PolicyConfig":
        if not self.allowed_packages:
            raise ValueError("allowed_packages must not be empty.")
        return self
