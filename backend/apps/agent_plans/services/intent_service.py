"""
IntentService — converts a raw transcript into a structured IntentResult via LLM.

The LLM is called with a strict system prompt that:
  - Enumerates the supported apps and goal types
  - Demands JSON-only output with a fixed schema
  - Sets confidence/ambiguity expectations

If the LLM is unavailable or returns a malformed response the service
falls back to keyword detection so the rest of the pipeline never stalls.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Optional

from apps.agent_core.llm_client import LLMClient, LLMError

logger = logging.getLogger(__name__)

# ── Known apps ────────────────────────────────────────────────────────────────

SUPPORTED_APPS: dict[str, str] = {
    "com.whatsapp":                    "WhatsApp (messaging)",
    "com.google.android.apps.maps":    "Google Maps (navigation / directions)",
    "com.android.chrome":              "Chrome (web browser / URL / search)",
    "com.google.android.gm":           "Gmail (email composition)",
    "com.supercell.brawlstars":        "Brawl Stars (game launcher)",
}

SUPPORTED_GOAL_TYPES: list[str] = [
    "send_message",        # send a chat/SMS message to a recipient
    "open_app",            # simply open an application
    "navigate_to",         # start GPS navigation to a destination
    "start_navigation",    # alias for navigate_to
    "search",              # search inside an app or on the web
    "draft_email",         # compose and send an email
    "open_website",        # open a URL in the browser
]

# ── Intent result dataclass ───────────────────────────────────────────────────

@dataclass
class IntentResult:
    goal: str                               # structured description (max 200 chars)
    goal_type: str                          # one of SUPPORTED_GOAL_TYPES
    app_package: str                        # e.g. "com.whatsapp"
    target_app: str                         # human-readable name e.g. "WhatsApp"
    entities: dict = field(default_factory=dict)  # recipient, message, destination, etc.
    risk_level: str = "low"                 # low | medium | high
    confidence: float = 1.0                 # 0.0 – 1.0
    ambiguity_flags: list = field(default_factory=list)
    raw_llm_response: str = ""              # raw JSON string from LLM for debugging

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_ambiguous(self) -> bool:
        return self.confidence < 0.5 or bool(self.ambiguity_flags)


# ── System prompt ─────────────────────────────────────────────────────────────

def _build_system_prompt(supported_packages: list[str]) -> str:
    # Only include apps that the device reports as installed
    apps_section = "\n".join(
        f'  - "{pkg}": {desc}'
        for pkg, desc in SUPPORTED_APPS.items()
        if pkg in supported_packages
    ) or "\n".join(f'  - "{k}": {v}' for k, v in SUPPORTED_APPS.items())

    goal_types_section = "\n".join(f"  - {g}" for g in SUPPORTED_GOAL_TYPES)

    return f"""You are an intent parser for a mobile automation system.
Your ONLY job is to read a user's voice or text command and return a JSON object.

SUPPORTED APPS (package name: description):
{apps_section}

SUPPORTED GOAL TYPES:
{goal_types_section}

OUTPUT RULES (non-negotiable):
1. Output ONLY a JSON object. No prose. No markdown fences. No explanation.
2. "goal" must be a concise structured description (max 100 chars), NOT the raw transcript.
   Good: "Send WhatsApp message 'running late' to Alice"
   Bad:  "Hey can you send a message to alice on whatsapp saying im running late"
3. "target_app" MUST be one of the exact package names listed above.
4. "goal_type" MUST be one of the exact goal type strings listed above.
5. "entities" extracts only what is explicitly stated. If unknown, omit the field.
6. "risk_level":
   - "low"    → reading, searching, opening apps
   - "medium" → navigation, web browsing, non-destructive actions
   - "high"   → sending messages, composing emails, actions that cannot be undone
7. "confidence" is 0.0–1.0. Use < 0.5 if the target app or goal is genuinely ambiguous.
8. "ambiguity_flags" lists specific things you are uncertain about.

REQUIRED OUTPUT SCHEMA:
{{
  "goal": "string, max 100 chars",
  "goal_type": "one of the supported goal types",
  "target_app": "exact package name",
  "entities": {{
    "recipient": "optional string",
    "message": "optional string",
    "destination": "optional string",
    "query": "optional string",
    "url": "optional string",
    "subject": "optional string",
    "body": "optional string"
  }},
  "risk_level": "low | medium | high",
  "confidence": 0.0,
  "ambiguity_flags": []
}}"""


# ── IntentService ─────────────────────────────────────────────────────────────

class IntentService:
    """
    Parses a natural-language transcript into a structured IntentResult.

    Uses LLMClient.from_settings() by default.
    Falls back to keyword detection if the LLM is unavailable.
    """

    def __init__(
        self,
        client: Optional[LLMClient] = None,
        reasoning_provider: Optional[str] = None,
    ) -> None:
        self._client = client  # None = lazy-init from settings on first call
        self._reasoning_provider = reasoning_provider
        self._fallback_service = _KeywordFallback()

    @property
    def client(self) -> LLMClient:
        if self._client is None:
            if self._reasoning_provider:
                self._client = LLMClient.from_reasoning_provider(
                    self._reasoning_provider
                )
            else:
                self._client = LLMClient.from_settings()
        return self._client

    def parse_intent(
        self,
        transcript: str,
        supported_packages: list[str] | None = None,
    ) -> IntentResult:
        """
        Parse the transcript and return an IntentResult.

        Never raises — falls back to keyword detection on LLM failure.
        """
        packages = supported_packages or list(SUPPORTED_APPS.keys())
        system_prompt = _build_system_prompt(packages)
        user_prompt = f'User command: "{transcript}"'

        raw_response = ""
        try:
            result_dict = self.client.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                json_mode=True,
            )
            raw_response = json.dumps(result_dict)
            parsed = self._parse_llm_result(result_dict, transcript, raw_response)
            if parsed.app_package and parsed.goal:
                return parsed

            fallback = self._fallback_service.parse(transcript)
            fallback.raw_llm_response = raw_response
            fallback.ambiguity_flags = list(parsed.ambiguity_flags) + [
                "LLM returned incomplete intent data — used keyword detection fallback"
            ]
            fallback.confidence = min(fallback.confidence, parsed.confidence, 0.6)
            return fallback

        except LLMError as exc:
            logger.warning(
                "LLM unavailable, falling back to keyword detection: %s", exc
            )
            fallback = self._fallback_service.parse(transcript)
            fallback.raw_llm_response = f"LLM_ERROR: {exc}"
            fallback.ambiguity_flags.append(
                f"LLM unavailable ({type(exc).__name__}) — used keyword detection"
            )
            fallback.confidence = min(fallback.confidence, 0.6)
            return fallback

    @staticmethod
    def parse(transcript: str) -> dict:
        """
        Legacy compatibility shim used by old views.
        Returns a plain dict (not IntentResult).
        """
        result = IntentService().parse_intent(transcript)
        return result.to_dict()

    # ── Private helpers ────────────────────────────────────────────────────────

    def _parse_llm_result(
        self,
        data: dict,
        transcript: str,
        raw_response: str,
    ) -> IntentResult:
        goal_type = str(data.get("goal_type", "")).strip()
        app_package = str(data.get("target_app", "")).strip()
        ambiguity: list[str] = list(data.get("ambiguity_flags", []))
        confidence: float = float(data.get("confidence", 1.0))

        # Validate goal_type
        if goal_type not in SUPPORTED_GOAL_TYPES:
            ambiguity.append(f"Unknown goal_type '{goal_type}' — using 'open_app' as default")
            goal_type = "open_app"
            confidence = min(confidence, 0.4)

        # Validate app_package
        if app_package not in SUPPORTED_APPS:
            ambiguity.append(f"Unknown app package '{app_package}'")
            confidence = min(confidence, 0.4)
            app_package = ""

        # Normalise entities — strip empties
        raw_entities: dict = data.get("entities", {}) or {}
        entities = {k: v for k, v in raw_entities.items() if v}

        goal = str(data.get("goal", transcript[:100])).strip()[:200]

        return IntentResult(
            goal=goal,
            goal_type=goal_type,
            app_package=app_package,
            target_app=SUPPORTED_APPS.get(app_package, app_package),
            entities=entities,
            risk_level=str(data.get("risk_level", "low")),
            confidence=max(0.0, min(1.0, confidence)),
            ambiguity_flags=ambiguity,
            raw_llm_response=raw_response,
        )


# ── Keyword fallback ──────────────────────────────────────────────────────────

class _KeywordFallback:
    """
    Simple keyword-based intent detection used when the LLM is unavailable.
    Accuracy is limited — the LLM path is always preferred.
    """

    def parse(self, transcript: str) -> IntentResult:
        lower = transcript.lower()
        app_package, target_app, goal_type, risk = self._detect(lower)
        entities = self._extract_entities(lower, goal_type)

        goal = self._build_goal(goal_type, app_package, entities, transcript)

        return IntentResult(
            goal=goal,
            goal_type=goal_type,
            app_package=app_package,
            target_app=target_app,
            entities=entities,
            risk_level=risk,
            confidence=0.6,
        )

    @staticmethod
    def _detect(lower: str) -> tuple[str, str, str, str]:
        import re
        has_url = bool(re.search(r"https?://|www\.|\.com|\.org|\.net|\.io", lower))

        if "whatsapp" in lower:
            goal_type = "send_message" if any(
                w in lower for w in ("send", "message", "tell", "say", "write")
            ) else "open_app"
            return "com.whatsapp", "WhatsApp", goal_type, "high" if goal_type == "send_message" else "medium"
        if any(w in lower for w in ("gmail", "email", "send email", "draft")):
            return "com.google.android.gm", "Gmail", "draft_email", "high"
        # Chrome: explicit browser keywords OR URL/domain detected
        if any(w in lower for w in ("chrome", "browser", "open website", "go to website")) or has_url:
            goal_type = "open_website" if has_url else "search"
            return "com.android.chrome", "Chrome", goal_type, "medium"
        if any(w in lower for w in ("maps", "navigate", "direction", "route", "get to")):
            return "com.google.android.apps.maps", "Google Maps", "navigate_to", "medium"
        if "brawl stars" in lower or "brawlstars" in lower or "brawl star" in lower:
            return "com.supercell.brawlstars", "Brawl Stars", "open_app", "low"
        if any(w in lower for w in ("search", "google", "look up", "find")):
            return "com.android.chrome", "Chrome", "search", "low"
        return "", "unknown", "open_app", "low"

    @staticmethod
    def _extract_entities(lower: str, goal_type: str) -> dict:
        entities: dict = {}
        if goal_type == "send_message":
            recipient = None

            # Pattern: "send/message/tell [name] on/via whatsapp ..."
            for kw in ("send ", "message ", "tell ", "whatsapp "):
                idx = lower.find(kw)
                if idx != -1:
                    rest = lower[idx + len(kw):].split()
                    if rest:
                        candidate = rest[0].strip(",'\"")
                        # Ignore functional words
                        if candidate not in ("me", "to", "a", "an", "the", "on", "via"):
                            recipient = candidate
                    break

            if recipient:
                entities["recipient"] = recipient
                # Everything after "whatsapp" / after the recipient is the message,
                # skipping connector words: "on", "via", "saying", "that", "to"
                msg_start = -1
                for kw in ("saying ", "that ", ": "):
                    idx = lower.find(kw)
                    if idx != -1:
                        msg_start = idx + len(kw)
                        break

                if msg_start == -1:
                    # Try to infer message as text after "whatsapp [recipient] [on whatsapp]"
                    for skip in (f"whatsapp {recipient} ", f"{recipient} on whatsapp "):
                        idx = lower.find(skip)
                        if idx != -1:
                            msg_start = idx + len(skip)
                            break

                if msg_start != -1:
                    msg = lower[msg_start:].strip(" '\"\n")
                    if msg:
                        entities["message"] = msg
        if goal_type in ("navigate_to", "start_navigation"):
            for kw in ("to ", "navigate to ", "go to ", "get to ", "directions to "):
                idx = lower.find(kw)
                if idx != -1:
                    entities["destination"] = lower[idx + len(kw):].strip()
                    break
        if goal_type == "search":
            for kw in ("search for ", "search ", "look up ", "find ", "google "):
                idx = lower.find(kw)
                if idx != -1:
                    entities["query"] = lower[idx + len(kw):].strip()
                    break
        if goal_type == "open_website":
            import re
            m = re.search(r"(https?://\S+|www\.\S+|\S+\.(?:com|org|net|io|co\.uk|dev))", lower)
            if m:
                entities["url"] = m.group(1)
        if goal_type == "draft_email":
            import re
            # Extract email address as recipient
            m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", lower)
            if m:
                entities["recipient"] = m.group(0)
            # Extract body after "saying", "that", "with body"
            for kw in ("saying ", "that ", "with body ", "body: "):
                idx = lower.find(kw)
                if idx != -1:
                    entities["body"] = lower[idx + len(kw):].strip()
                    break
        return entities

    @staticmethod
    def _build_goal(goal_type: str, app: str, entities: dict, transcript: str) -> str:
        if goal_type == "send_message":
            recipient = entities.get("recipient", "contact")
            msg = entities.get("message", "")
            preview = f": '{msg[:40]}'" if msg else ""
            return f"Send WhatsApp message to {recipient}{preview}"
        if goal_type in ("navigate_to", "start_navigation"):
            dest = entities.get("destination", "destination")
            return f"Navigate to {dest}"
        if goal_type == "search":
            q = entities.get("query", "")
            return f"Search for '{q}'"
        if goal_type == "draft_email":
            return f"Draft email"
        return transcript[:100]
