"""
VisionReasoningService: screenshot-based reasoning for the vision fallback path.

Two capabilities, both deliberately narrow:
  find_tap_target   — "where on screen do I tap to do X?" → coordinates
  verify_tap_effect — "did the tap produce the expected change?" → bool

Neither method plans goals nor chooses action types; those remain in
StepReasoningService.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from apps.agent_core.llm_client import LLMClient, LLMError

logger = logging.getLogger(__name__)

# ── Confidence threshold below which a result is discarded ──────────────────
_MIN_CONFIDENCE = 0.4

VISION_VERIFY_SYSTEM_PROMPT = """You are an Android UI state verifier.

You will receive a screenshot taken AFTER a tap action and a description of \
the expected result. Decide whether the screen reflects the expected change.

Rules:
1. Return ONLY a JSON object, nothing else.
2. "changed" must be true if the screen shows clear evidence of the expected \
result, false otherwise.
3. "observation" must be a single concise sentence describing what you see.

Output schema:
{
  "changed": true,
  "observation": "The chat thread is now open showing the conversation."
}"""

VISION_TAP_SYSTEM_PROMPT = """You are a visual Android UI element locator.

You will receive a screenshot of an Android screen and a description of \
what needs to be tapped. Identify the correct UI element and return its \
CENTER pixel coordinates.

Rules:
1. Return ONLY a JSON object, nothing else.
2. x is the horizontal pixel from the left edge.
3. y is the vertical pixel from the top edge.
4. confidence: 0.0 if you cannot find the element, 1.0 if certain.
5. If multiple candidates exist, pick the most prominent one.
6. Never return coordinates outside the reported screen dimensions.

Output schema:
{
  "x": 540,
  "y": 1200,
  "description": "Large blue circular shutter button at bottom center",
  "confidence": 0.88,
  "reasoning": "The shutter button is the large circular element..."
}"""


@dataclass
class VisionTapTarget:
    x: int
    y: int
    description: str
    confidence: float
    reasoning: str


class VisionReasoningService:
    """
    Wraps an LLMClient to locate a tap target from a screenshot.

    Returns None (never raises) when:
      - The LLM call fails
      - The response is malformed
      - Confidence is below _MIN_CONFIDENCE (0.4)
    """

    def __init__(
        self,
        client: Optional[LLMClient] = None,
        reasoning_provider: Optional[str] = None,
    ) -> None:
        if client is not None:
            self._llm = client
        elif reasoning_provider:
            self._llm = LLMClient.from_reasoning_provider(reasoning_provider)
        else:
            self._llm = LLMClient.from_settings()

    def find_tap_target(
        self,
        goal_description: str,
        element_hint: str,
        screenshot_b64: str,
        screen_width: int,
        screen_height: int,
    ) -> Optional[VisionTapTarget]:
        """
        Ask the VLM to locate element_hint on the screenshot and return
        centre coordinates. Returns None if the model cannot find the element
        with sufficient confidence.

        Args:
            goal_description: high-level task context (e.g. "send a WhatsApp message")
            element_hint:      what to find (e.g. "search icon in the top bar")
            screenshot_b64:    JPEG screenshot as a base64 string
            screen_width:      device display width in pixels
            screen_height:     device display height in pixels
        """
        user_prompt = (
            f"Screen size: {screen_width}x{screen_height}px\n"
            f"Task: {goal_description}\n"
            f"Find and return coordinates for: {element_hint}"
        )

        try:
            raw = self._llm.generate(
                system_prompt=VISION_TAP_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                json_mode=True,
                image_b64=screenshot_b64,
            )
        except LLMError as exc:
            logger.warning(
                "VisionReasoningService: LLM call failed hint=%r: %s",
                element_hint, exc,
            )
            return None

        result = self._parse_response(raw, screen_width, screen_height)
        if result is None:
            logger.warning(
                "VisionReasoningService: unusable response hint=%r raw=%r",
                element_hint, raw,
            )
        else:
            logger.debug(
                "VisionReasoningService: tap target x=%d y=%d confidence=%.2f hint=%r description=%r",
                result.x, result.y, result.confidence, element_hint, result.description,
            )
        return result

    def verify_tap_effect(
        self,
        before_b64: str,          # noqa: ARG002 — reserved for multi-image models
        after_b64: str,
        expected_change: str,
    ) -> bool:
        """
        Ask the VLM whether the screen changed as expected after a tap.

        Only the after-screenshot is sent to the model; current single-image VL
        models cannot compare two images in one call.  The before-screenshot is
        accepted as a parameter so callers are future-proof when multi-image
        support is added.

        Returns True  if the model confirms the expected change occurred.
        Returns False on LLM error, malformed response, or "changed": false.
        """
        user_prompt = (
            f"I tapped a button. Expected result: {expected_change}\n"
            "Did the screen change as expected? "
            'Answer with {"changed": true/false, "observation": "..."}'
        )

        try:
            raw = self._llm.generate(
                system_prompt=VISION_VERIFY_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                json_mode=True,
                image_b64=after_b64,
            )
        except LLMError as exc:
            logger.warning(
                "VisionReasoningService.verify_tap_effect: LLM call failed: %s", exc,
            )
            return False

        if not isinstance(raw, dict):
            return False

        changed = bool(raw.get("changed", False))
        observation = str(raw.get("observation", ""))
        logger.debug(
            "VisionReasoningService.verify_tap_effect: changed=%s observation=%r",
            changed, observation,
        )
        return changed

    def _parse_response(
        self,
        raw: object,
        screen_width: int,
        screen_height: int,
    ) -> Optional[VisionTapTarget]:
        if not isinstance(raw, dict):
            return None
        try:
            x = int(raw["x"])
            y = int(raw["y"])
            confidence = float(raw.get("confidence", 0.0))
        except (KeyError, TypeError, ValueError):
            return None

        # Clamp to screen bounds regardless of what the model returned
        x = max(0, min(x, screen_width - 1))
        y = max(0, min(y, screen_height - 1))

        if confidence < _MIN_CONFIDENCE:
            return None

        return VisionTapTarget(
            x=x,
            y=y,
            description=str(raw.get("description", "")),
            confidence=confidence,
            reasoning=str(raw.get("reasoning", "")),
        )
