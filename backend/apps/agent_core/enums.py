from enum import Enum


class ActionType(str, Enum):
    OPEN_APP = "OPEN_APP"
    GET_SCREEN_STATE = "GET_SCREEN_STATE"
    WAIT_FOR_APP = "WAIT_FOR_APP"
    WAIT_FOR_ELEMENT = "WAIT_FOR_ELEMENT"
    FIND_ELEMENT = "FIND_ELEMENT"
    TAP_ELEMENT = "TAP_ELEMENT"
    LONG_PRESS_ELEMENT = "LONG_PRESS_ELEMENT"
    FOCUS_ELEMENT = "FOCUS_ELEMENT"
    TYPE_TEXT = "TYPE_TEXT"
    CLEAR_TEXT = "CLEAR_TEXT"
    SCROLL = "SCROLL"
    SWIPE = "SWIPE"
    BACK = "BACK"
    HOME = "HOME"
    REQUEST_CONFIRMATION = "REQUEST_CONFIRMATION"
    ASSERT_SCREEN = "ASSERT_SCREEN"
    ASSERT_ELEMENT = "ASSERT_ELEMENT"
    ABORT = "ABORT"
    TAP_COORDINATES = "TAP_COORDINATES"
    GET_SCREENSHOT = "GET_SCREENSHOT"


class ActionSensitivity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionResultStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    ABORTED = "aborted"
    SKIPPED = "skipped"


class ActionErrorCode(str, Enum):
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    ELEMENT_NOT_CLICKABLE = "ELEMENT_NOT_CLICKABLE"
    APP_NOT_FOREGROUND = "APP_NOT_FOREGROUND"
    SCREEN_MISMATCH = "SCREEN_MISMATCH"
    TIMEOUT = "TIMEOUT"
    SENSITIVE_SCREEN = "SENSITIVE_SCREEN"
    CONFIRMATION_REJECTED = "CONFIRMATION_REJECTED"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    UNKNOWN = "UNKNOWN"


class ConfirmationStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SensitiveScreenPolicy(str, Enum):
    ABORT = "abort"
    PAUSE = "pause"


class ScrollDirection(str, Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
