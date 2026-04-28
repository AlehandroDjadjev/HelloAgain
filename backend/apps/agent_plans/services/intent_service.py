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
    "com.viber.voip":                  "Viber (messaging / calls)",
    "org.telegram.messenger":          "Telegram (messaging)",
    "com.facebook.orca":               "Messenger (messaging)",
    "com.google.android.apps.messaging": "Google Messages (SMS / RCS messaging)",
    "com.google.android.apps.maps":    "Google Maps (navigation / directions)",
    "com.android.chrome":              "Chrome (web browser / URL / search)",
    "com.google.android.gm":           "Gmail (email composition)",
    "com.supercell.brawlstars":        "Brawl Stars (game launcher)",
}


def _describe_supported_app(package_name: str) -> str:
    if package_name in SUPPORTED_APPS:
        return SUPPORTED_APPS[package_name]

    lower = package_name.lower()
    if any(token in lower for token in ("viber", "whatsapp", "telegram", "messenger", "messages", "sms")):
        return "Messaging app"
    if any(token in lower for token in ("maps", "navigation", "waze")):
        return "Navigation / directions app"
    if any(token in lower for token in ("chrome", "browser", "firefox", "edge")):
        return "Web browser / search app"
    if any(token in lower for token in ("gmail", "mail", "email")):
        return "Email app"
    return "Installed Android app"


def _package_category(package_name: str) -> str:
    description = _describe_supported_app(package_name).lower()
    lower = package_name.lower()
    if "messaging" in description or any(
        token in lower
        for token in ("viber", "whatsapp", "telegram", "messenger", "messages", "sms")
    ):
        return "messaging"
    if "navigation" in description or any(token in lower for token in ("maps", "navigation", "waze")):
        return "navigation"
    if "browser" in description or "search" in description or any(
        token in lower for token in ("chrome", "browser", "firefox", "edge")
    ):
        return "browser"
    if "email" in description or any(token in lower for token in ("gmail", "mail", "email")):
        return "email"
    return "app"


def _category_for_goal_type(goal_type: str) -> str:
    if goal_type == "send_message":
        return "messaging"
    if goal_type in ("navigate_to", "start_navigation"):
        return "navigation"
    if goal_type in ("search", "open_website"):
        return "browser"
    if goal_type == "draft_email":
        return "email"
    return "app"


def _pick_package_for_category(
    supported_packages: list[str],
    category: str,
    transcript: str = "",
) -> tuple[str, str]:
    packages = supported_packages or list(SUPPORTED_APPS.keys())
    lower_transcript = transcript.lower()
    candidates = [
        package
        for package in packages
        if _package_category(package) == category
    ]
    if not candidates:
        return "", ""

    for package in candidates:
        package_tail = package.rsplit(".", 1)[-1].lower()
        if package_tail and package_tail in lower_transcript:
            return package, _describe_supported_app(package).split(" (", 1)[0]

    if len(candidates) == 1:
        package = candidates[0]
        return package, _describe_supported_app(package).split(" (", 1)[0]

    preferred = {
        "messaging": [
            "com.viber.voip",
            "com.whatsapp",
            "org.telegram.messenger",
            "com.google.android.apps.messaging",
            "com.facebook.orca",
        ],
        "navigation": [
            "com.google.android.apps.maps",
            "com.waze",
        ],
        "browser": [
            "com.android.chrome",
            "org.mozilla.firefox",
            "com.microsoft.emmx",
        ],
        "email": [
            "com.google.android.gm",
        ],
    }.get(category, [])
    for package in preferred:
        if package in candidates:
            return package, _describe_supported_app(package).split(" (", 1)[0]

    package = candidates[0]
    return package, _describe_supported_app(package).split(" (", 1)[0]

SUPPORTED_GOAL_TYPES: list[str] = [
    "send_message",        # send a chat/SMS message to a recipient
    "open_app",            # simply open an application
    "navigate_to",         # start GPS navigation to a destination
    "start_navigation",    # alias for navigate_to
    "search",              # search inside an app or on the web
    "draft_email",         # compose and send an email
    "open_website",        # open a URL in the browser
    "invalid_request",     # not a valid phone automation request
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
    package_list = supported_packages or list(SUPPORTED_APPS.keys())

    apps_section = "\n".join(
        f'  - "{pkg}": {_describe_supported_app(pkg)}'
        for pkg in package_list
    )

    goal_types_section = "\n".join(f"  - {g}" for g in SUPPORTED_GOAL_TYPES)

    return f"""You are an intent parser for a mobile automation system.
You are preparing commands for an action model that waits for clear phone automation requests.
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
7. If the command is clearly a phone action but the user did not name an app,
   choose the best installed app from SUPPORTED APPS based on the task:
   messaging -> a messaging app, navigation -> a maps app, search/website -> a browser,
   email -> an email app. Add "target_app_inferred" to ambiguity_flags.
8. "confidence" is 0.0–1.0. Use < 0.5 only if the task itself is unsafe or genuinely unclear.
9. "ambiguity_flags" lists specific things you are uncertain about.
10. If the user input is chit-chat, nonsense, a general knowledge question, not actionable on the phone,
   or too unclear to execute safely, return:
   - "goal_type": "invalid_request"
   - "target_app": ""
   - "risk_level": "low"
   - "confidence": 0.0-0.35
   - "ambiguity_flags": ["not_actionable_request"] or another specific reason
11. Do NOT mark a real phone task invalid just because the app was not named.
    Use invalid_request only when the requested action itself is not executable on the phone.

REQUIRED OUTPUT SCHEMA:
{{
  "goal": "string, max 100 chars",
  "goal_type": "one of the supported goal types",
  "target_app": "exact package name, or empty string for invalid_request",
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
            parsed = self._parse_llm_result(
                result_dict,
                transcript,
                raw_response,
                allowed_packages=packages,
            )
            if parsed.goal_type == "invalid_request":
                return parsed

            if parsed.app_package and parsed.goal:
                return parsed

            fallback = self._fallback_service.parse(transcript, packages)
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
            fallback = self._fallback_service.parse(transcript, packages)
            fallback.raw_llm_response = f"LLM_ERROR: {exc}"
            fallback.ambiguity_flags.append(
                f"LLM unavailable ({type(exc).__name__}) — used keyword detection"
            )
            fallback.confidence = min(fallback.confidence, 0.6)
            return fallback

    @staticmethod
    def parse_navigation_only(transcript: str) -> IntentResult:
        """
        Deterministic navigation-only parser.

        This intentionally bypasses the LLM/Qwen path and uses the keyword
        fallback logic so navigator flows stay API-only and predictable.
        """
        fallback = _KeywordFallback().parse(transcript)
        destination = str((fallback.entities or {}).get("destination", "")).strip()
        is_valid_navigation = (
            fallback.goal_type in ("navigate_to", "start_navigation")
            and fallback.app_package == "com.google.android.apps.maps"
            and destination != ""
        )
        if is_valid_navigation:
            fallback.goal_type = "navigate_to"
            fallback.target_app = "Google Maps"
            fallback.risk_level = "medium"
            fallback.confidence = max(fallback.confidence, 0.92)
            fallback.ambiguity_flags = [
                flag for flag in fallback.ambiguity_flags
                if flag not in {"not_actionable_request"}
            ]
            fallback.raw_llm_response = "navigation_api_only"
            return fallback

        return IntentResult(
            goal="No actionable navigation command",
            goal_type="invalid_request",
            app_package="",
            target_app="",
            entities={},
            risk_level="low",
            confidence=0.2,
            ambiguity_flags=["not_navigation_request"],
            raw_llm_response="navigation_api_only",
        )

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
        allowed_packages: list[str] | None = None,
    ) -> IntentResult:
        goal_type = str(data.get("goal_type", "")).strip()
        app_package = str(data.get("target_app", "")).strip()
        ambiguity: list[str] = list(data.get("ambiguity_flags", []))
        confidence: float = float(data.get("confidence", 1.0))
        allowed_package_set = set(allowed_packages or SUPPORTED_APPS.keys())

        # Validate goal_type
        if goal_type not in SUPPORTED_GOAL_TYPES:
            ambiguity.append(
                f"Unknown goal_type '{goal_type}' — treating as invalid request"
            )
            goal_type = "invalid_request"
            confidence = min(confidence, 0.2)

        if goal_type == "invalid_request":
            app_package = ""
            if "not_actionable_request" not in ambiguity:
                ambiguity.append("not_actionable_request")
            confidence = min(confidence, 0.35)
        # Validate app_package
        elif app_package not in allowed_package_set:
            inferred_package, _ = _pick_package_for_category(
                list(allowed_package_set),
                _category_for_goal_type(goal_type),
                transcript,
            )
            if inferred_package:
                ambiguity.append(
                    f"Unknown app package '{app_package}' - inferred '{inferred_package}' from installed apps"
                )
                confidence = min(confidence, 0.72)
                app_package = inferred_package
            else:
                ambiguity.append(f"Unknown app package '{app_package}'")
                confidence = min(confidence, 0.4)
                app_package = ""

        # Normalise entities — strip empties
        raw_entities: dict = data.get("entities", {}) or {}
        entities = {k: v for k, v in raw_entities.items() if v}

        goal = str(data.get("goal", transcript[:100])).strip()[:200]
        if goal_type == "invalid_request" and not goal:
            goal = "No actionable phone command"

        return IntentResult(
            goal=goal,
            goal_type=goal_type,
            app_package=app_package,
            target_app=SUPPORTED_APPS.get(app_package, app_package),
            entities=entities,
            risk_level="low" if goal_type == "invalid_request" else str(data.get("risk_level", "low")),
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

    def parse(
        self,
        transcript: str,
        supported_packages: list[str] | None = None,
    ) -> IntentResult:
        lower = transcript.lower()
        app_package, target_app, goal_type, risk = self._detect(
            lower,
            supported_packages=supported_packages or list(SUPPORTED_APPS.keys()),
        )
        entities = self._extract_entities(lower, goal_type)

        goal = self._build_goal(goal_type, app_package, entities, transcript)

        ambiguity_flags = (
            ["not_actionable_request"] if goal_type == "invalid_request" else []
        )
        confidence = 0.2 if goal_type == "invalid_request" else 0.6

        return IntentResult(
            goal=goal,
            goal_type=goal_type,
            app_package=app_package,
            target_app=target_app,
            entities=entities,
            risk_level=risk,
            confidence=confidence,
            ambiguity_flags=ambiguity_flags,
        )

    @staticmethod
    def _detect(
        lower: str,
        supported_packages: list[str],
    ) -> tuple[str, str, str, str]:
        import re
        has_url = bool(re.search(r"https?://|www\.|\.com|\.org|\.net|\.io", lower))

        if "whatsapp" in lower:
            goal_type = "send_message" if any(
                w in lower for w in ("send", "message", "tell", "say", "write", "text")
            ) else "open_app"
            return "com.whatsapp", "WhatsApp", goal_type, "high" if goal_type == "send_message" else "medium"
        if "viber" in lower:
            goal_type = "send_message" if any(
                w in lower for w in ("send", "message", "tell", "say", "write", "text")
            ) else "open_app"
            return "com.viber.voip", "Viber", goal_type, "high" if goal_type == "send_message" else "medium"
        if "telegram" in lower:
            goal_type = "send_message" if any(
                w in lower for w in ("send", "message", "tell", "say", "write", "text")
            ) else "open_app"
            return "org.telegram.messenger", "Telegram", goal_type, "high" if goal_type == "send_message" else "medium"
        has_message_verb = any(
            w in lower
            for w in ("send", "message", "say", "write", "text", "chat")
        ) or ("tell " in lower and "tell me " not in lower)
        if has_message_verb:
            package, label = _pick_package_for_category(
                supported_packages,
                "messaging",
                lower,
            )
            if package:
                return package, label, "send_message", "high"
        if any(w in lower for w in ("gmail", "email", "send email", "draft")):
            package, label = _pick_package_for_category(
                supported_packages,
                "email",
                lower,
            )
            return package or "com.google.android.gm", label or "Gmail", "draft_email", "high"
        # Chrome: explicit browser keywords OR URL/domain detected
        if any(w in lower for w in ("chrome", "browser", "open website", "go to website")) or has_url:
            goal_type = "open_website" if has_url else "search"
            package, label = _pick_package_for_category(
                supported_packages,
                "browser",
                lower,
            )
            return package or "com.android.chrome", label or "Chrome", goal_type, "medium"
        if any(
            w in lower
            for w in (
                "maps",
                "navigate",
                "direction",
                "route",
                "get to",
                "take me to",
                "drive to",
                "bring me to",
                "заведи ме до",
                "закарай ме до",
                "отведи ме до",
            )
        ):
            package, label = _pick_package_for_category(
                supported_packages,
                "navigation",
                lower,
            )
            return package or "com.google.android.apps.maps", label or "Google Maps", "navigate_to", "medium"
        if "brawl stars" in lower or "brawlstars" in lower or "brawl star" in lower:
            return "com.supercell.brawlstars", "Brawl Stars", "open_app", "low"
        if any(w in lower for w in ("search", "google", "look up", "find")):
            package, label = _pick_package_for_category(
                supported_packages,
                "browser",
                lower,
            )
            return package or "com.android.chrome", label or "Chrome", "search", "low"
        return "", "unknown", "invalid_request", "low"

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
            for kw in (
                "navigate to ",
                "go to ",
                "get to ",
                "directions to ",
                "take me to ",
                "drive to ",
                "bring me to ",
                "заведи ме до ",
                "закарай ме до ",
                "отведи ме до ",
                "to ",
            ):
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
            app_label = _describe_supported_app(app).split(" (", 1)[0] if app else "message"
            return f"Send {app_label} message to {recipient}{preview}"
        if goal_type in ("navigate_to", "start_navigation"):
            dest = entities.get("destination", "destination")
            return f"Navigate to {dest}"
        if goal_type == "search":
            q = entities.get("query", "")
            return f"Search for '{q}'"
        if goal_type == "draft_email":
            return f"Draft email"
        if goal_type == "invalid_request":
            return "No actionable phone command"
        return transcript[:100]
