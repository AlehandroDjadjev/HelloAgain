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
    "com.google.android.apps.maps":    "Google Maps (navigation / directions)",
    "com.android.chrome":              "Chrome (web browser / URL / search)",
    "com.google.android.gm":           "Gmail (email composition)",
    "com.supercell.brawlstars":        "Brawl Stars (game launcher)",
}


def _describe_supported_app(package_name: str) -> str:
    return SUPPORTED_APPS.get(package_name, "Installed Android app")

SUPPORTED_GOAL_TYPES: list[str] = [
    "send_message",        # send a chat/SMS message to a recipient
    "open_app",            # simply open an application
    "navigate_to",         # start GPS navigation to a destination
    "start_navigation",    # alias for navigate_to
    "search",              # search inside an app or on the web
    "draft_email",         # compose and send an email
    "open_website",        # open a URL in the browser
    "continue_in_app",     # continue inside the currently open app / screen
    "scroll_view",         # scroll the current screen inside the current app
    "browse_current_view", # inspect / open items already visible on the current screen
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
    needs_clarification: bool = False
    missing_fields: list[str] = field(default_factory=list)
    clarification_question: str = ""
    raw_llm_response: str = ""              # raw JSON string from LLM for debugging

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_ambiguous(self) -> bool:
        return self.confidence < 0.5 or bool(self.ambiguity_flags)


# ── System prompt ─────────────────────────────────────────────────────────────

def _build_system_prompt(
    supported_packages: list[str],
    context: dict | None = None,
) -> str:
    package_list = supported_packages or list(SUPPORTED_APPS.keys())

    apps_section = "\n".join(
        f'  - "{pkg}": {_describe_supported_app(pkg)}'
        for pkg in package_list
    )

    goal_types_section = "\n".join(f"  - {g}" for g in SUPPORTED_GOAL_TYPES)

    context = _normalize_context(context)
    context_section = ""
    if context:
        context_section = f"""

CURRENT SESSION CONTEXT:
- follow_up_mode: {context.get("follow_up_mode", False)}
- current_app_package: {context.get("current_app_package", "") or "(unknown)"}
- current_app_name: {context.get("current_app_name", "") or "(unknown)"}
- current_window_title: {context.get("current_window_title", "") or "(unknown)"}
- previous_goal: {context.get("previous_goal", "") or "(unknown)"}

FOLLOW-UP RULES:
- If the new instruction is short, relative, or refers to "here", "there", "this app", "the current page", "results", or "continue",
  interpret it relative to the current app and current screen when follow_up_mode=true.
- Reuse current_app_package if the user did not explicitly ask to switch apps.
- Do NOT ask for the app again when it is already clear from the current context.
"""

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
7. "confidence" is 0.0–1.0. Use < 0.5 if the target app or goal is genuinely ambiguous.
8. "ambiguity_flags" lists specific things you are uncertain about.
9. If the user input is chit-chat, nonsense, a general knowledge question, not actionable on the phone,
   or too unclear to execute safely, return:
   - "goal_type": "invalid_request"
   - "target_app": ""
   - "risk_level": "low"
   - "confidence": 0.0-0.35
   - "ambiguity_flags": ["not_actionable_request"] or another specific reason
10. Do NOT guess an app just to satisfy the schema. If the request is not clearly actionable,
    use "invalid_request".
11. For follow-up requests inside an already open app, prefer:
    - "continue_in_app"
    - "scroll_view"
    - "browse_current_view"
    instead of forcing a brand-new standalone command.
12. If an essential detail is missing, set "needs_clarification": true, include the missing fields,
    and provide exactly one short clarification question in the same language as the user's command.
13. If the request is already clear enough to execute, set "needs_clarification": false,
    "missing_fields": [], and "clarification_question": "".
{context_section}

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
  "ambiguity_flags": [],
  "needs_clarification": false,
  "missing_fields": [],
  "clarification_question": ""
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

    def _alternate_reasoning_provider(self) -> str:
        provider_name = str(
            getattr(self.client, "provider", "") or self._reasoning_provider or ""
        ).lower()
        if provider_name == "openai":
            return "local"
        if provider_name in {"transformers", "ollama", "local"}:
            return "openai"
        return ""

    def _try_alternate_llm(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[dict, str] | None:
        alternate_provider = self._alternate_reasoning_provider()
        if not alternate_provider:
            return None
        try:
            alternate_client = LLMClient.from_reasoning_provider(alternate_provider)
            result_dict = alternate_client.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                json_mode=True,
            )
        except LLMError:
            return None
        return result_dict, alternate_provider

    def parse_intent(
        self,
        transcript: str,
        supported_packages: list[str] | None = None,
        context: dict | None = None,
    ) -> IntentResult:
        """
        Parse the transcript and return an IntentResult.

        Never raises — falls back to keyword detection on LLM failure.
        """
        packages = supported_packages or list(SUPPORTED_APPS.keys())
        normalized_context = _normalize_context(context)
        system_prompt = _build_system_prompt(packages, normalized_context)
        user_prompt = self._build_user_prompt(transcript, normalized_context)

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
            parsed = self._merge_contextual_intent(
                parsed,
                transcript,
                normalized_context,
                allowed_packages=packages,
            )
            parsed = self._with_clarification_metadata(parsed)
            if parsed.goal_type == "invalid_request":
                return parsed

            if parsed.app_package and parsed.goal:
                return parsed

            fallback = self._with_clarification_metadata(
                self._fallback_service.parse(transcript)
            )
            fallback = self._merge_contextual_intent(
                fallback,
                transcript,
                normalized_context,
                allowed_packages=packages,
            )
            fallback.raw_llm_response = raw_response
            fallback.ambiguity_flags = list(parsed.ambiguity_flags) + [
                "LLM returned incomplete intent data — used keyword detection fallback"
            ]
            fallback.confidence = min(fallback.confidence, parsed.confidence, 0.6)
            return self._with_clarification_metadata(fallback)

        except LLMError as exc:
            alternate_result = self._try_alternate_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            if alternate_result is not None:
                result_dict, alternate_provider = alternate_result
                raw_response = json.dumps(result_dict)
                parsed = self._parse_llm_result(
                    result_dict,
                    transcript,
                    raw_response,
                    allowed_packages=packages,
                )
                parsed = self._merge_contextual_intent(
                    parsed,
                    transcript,
                    normalized_context,
                    allowed_packages=packages,
                )
                parsed = self._with_clarification_metadata(parsed)
                parsed.raw_llm_response = (
                    f"{raw_response}\nFALLBACK_PROVIDER:{alternate_provider}"
                )
                return parsed
            logger.warning(
                "LLM unavailable, falling back to keyword detection: %s", exc
            )
            fallback = self._with_clarification_metadata(
                self._fallback_service.parse(transcript)
            )
            fallback = self._merge_contextual_intent(
                fallback,
                transcript,
                normalized_context,
                allowed_packages=packages,
            )
            fallback.raw_llm_response = f"LLM_ERROR: {exc}"
            fallback.ambiguity_flags.append(
                f"LLM unavailable ({type(exc).__name__}) — used keyword detection"
            )
            fallback.confidence = min(fallback.confidence, 0.6)
            return self._with_clarification_metadata(fallback)

    @staticmethod
    def parse_navigation_only(transcript: str) -> IntentResult:
        """
        Deterministic navigation-only parser.

        This intentionally bypasses the LLM/Qwen path and uses the keyword
        fallback logic so navigator flows stay API-only and predictable.
        """
        fallback = IntentService._with_clarification_metadata(
            _KeywordFallback().parse(transcript)
        )
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
            return IntentService._with_clarification_metadata(fallback)

        return IntentService._with_clarification_metadata(IntentResult(
            goal="No actionable navigation command",
            goal_type="invalid_request",
            app_package="",
            target_app="",
            entities={},
            risk_level="low",
            confidence=0.2,
            ambiguity_flags=["not_navigation_request"],
            raw_llm_response="navigation_api_only",
        ))

    @staticmethod
    def _build_user_prompt(transcript: str, context: dict | None) -> str:
        if not context:
            return f'User command: "{transcript}"'

        return (
            f'User command: "{transcript}"\n'
            f'Current app package: "{context.get("current_app_package", "")}"\n'
            f'Current app name: "{context.get("current_app_name", "")}"\n'
            f'Current window title: "{context.get("current_window_title", "")}"\n'
            f'Previous goal: "{context.get("previous_goal", "")}"\n'
            f'Previous entities: {json.dumps(context.get("previous_entities", {}), ensure_ascii=False)}\n'
            f'Follow-up mode: {str(bool(context.get("follow_up_mode"))).lower()}'
        )

    def _merge_contextual_intent(
        self,
        result: IntentResult,
        transcript: str,
        context: dict | None,
        allowed_packages: list[str] | None = None,
    ) -> IntentResult:
        context = _normalize_context(context)
        if not context:
            return result

        current_app_package = str(context.get("current_app_package") or "").strip()
        current_app_name = str(context.get("current_app_name") or "").strip()
        follow_up_mode = bool(context.get("follow_up_mode"))
        if not follow_up_mode or not current_app_package:
            return result

        allowed_package_set = set(allowed_packages or SUPPORTED_APPS.keys())
        if current_app_package not in allowed_package_set:
            return result

        if (
            not result.app_package
            and result.goal_type in {
                "open_app",
                "search",
                "open_website",
                "send_message",
                "draft_email",
                "navigate_to",
                "start_navigation",
                "continue_in_app",
                "scroll_view",
                "browse_current_view",
            }
        ):
            result.app_package = current_app_package
            result.target_app = current_app_name or SUPPORTED_APPS.get(
                current_app_package,
                current_app_package,
            )
            if result.confidence < 0.72:
                result.confidence = 0.72

        return result

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
        elif app_package and app_package not in allowed_package_set:
            ambiguity.append(f"Unknown app package '{app_package}'")
            confidence = min(confidence, 0.4)
            app_package = ""

        # Normalise entities — strip empties
        raw_entities: dict = data.get("entities", {}) or {}
        entities = {k: v for k, v in raw_entities.items() if v}
        raw_missing_fields = data.get("missing_fields", []) or []
        missing_fields = [
            str(field_name).strip()
            for field_name in raw_missing_fields
            if str(field_name).strip()
        ]
        clarification_question = str(
            data.get("clarification_question", "")
        ).strip()[:200]
        needs_clarification = bool(data.get("needs_clarification"))

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
            needs_clarification=needs_clarification,
            missing_fields=missing_fields,
            clarification_question=clarification_question,
            raw_llm_response=raw_response,
        )

    @staticmethod
    def _with_clarification_metadata(result: IntentResult) -> IntentResult:
        if not result.missing_fields:
            result.missing_fields = _missing_fields_for_intent(result)
        else:
            deduped_missing_fields: list[str] = []
            for field_name in result.missing_fields:
                clean_name = str(field_name).strip()
                if clean_name and clean_name not in deduped_missing_fields:
                    deduped_missing_fields.append(clean_name)
            result.missing_fields = deduped_missing_fields
        result.clarification_question = str(result.clarification_question or "").strip()
        result.needs_clarification = bool(
            result.needs_clarification
            or result.clarification_question
            or result.missing_fields
            or _is_ambiguous_intent(result)
        )
        return result


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
    def _detect(lower: str) -> tuple[str, str, str, str]:
        import re

        has_url = bool(re.search(r"https?://|www\.|\.com|\.org|\.net|\.io", lower))

        if "whatsapp" in lower:
            goal_type = "send_message" if any(
                w in lower for w in ("send", "message", "tell", "say", "write")
            ) else "open_app"
            return (
                "com.whatsapp",
                "WhatsApp",
                goal_type,
                "high" if goal_type == "send_message" else "medium",
            )
        if "viber" in lower:
            goal_type = "send_message" if any(
                w in lower
                for w in ("send", "message", "tell", "say", "write", "прати", "съобщ")
            ) else "open_app"
            return (
                "com.viber.voip",
                "Viber",
                goal_type,
                "high" if goal_type == "send_message" else "medium",
            )
        if any(w in lower for w in ("gmail", "email", "send email", "draft")):
            return "com.google.android.gm", "Gmail", "draft_email", "high"
        if any(
            w in lower for w in ("scroll", "swipe", "превърти", "скрол", "надолу", "нагоре")
        ):
            return "", "current_app", "scroll_view", "low"
        if (
            any(
                w in lower
                for w in ("chrome", "browser", "open website", "go to website")
            )
            or has_url
        ):
            goal_type = "open_website" if has_url else "search"
            return "com.android.chrome", "Chrome", goal_type, "medium"
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
                "Ð·Ð°Ð²ÐµÐ´Ð¸ Ð¼Ðµ Ð´Ð¾",
                "Ð·Ð°ÐºÐ°Ñ€Ð°Ð¹ Ð¼Ðµ Ð´Ð¾",
                "Ð¾Ñ‚Ð²ÐµÐ´Ð¸ Ð¼Ðµ Ð´Ð¾",
            )
        ):
            return (
                "com.google.android.apps.maps",
                "Google Maps",
                "navigate_to",
                "medium",
            )
        if "brawl stars" in lower or "brawlstars" in lower or "brawl star" in lower:
            return "com.supercell.brawlstars", "Brawl Stars", "open_app", "low"
        if any(w in lower for w in ("search", "google", "look up", "find")):
            return "com.android.chrome", "Chrome", "search", "low"
        return "", "unknown", "invalid_request", "low"

    @staticmethod
    def _extract_entities(lower: str, goal_type: str) -> dict:
        entities: dict = {}
        if goal_type == "send_message":
            recipient = None

            for kw in ("send ", "message ", "tell ", "whatsapp "):
                idx = lower.find(kw)
                if idx != -1:
                    rest = lower[idx + len(kw):].split()
                    if rest:
                        candidate = rest[0].strip(",'\"")
                        if candidate not in ("me", "to", "a", "an", "the", "on", "via"):
                            recipient = candidate
                    break

            if recipient:
                entities["recipient"] = recipient
                msg_start = -1
                for kw in ("saying ", "that ", ": "):
                    idx = lower.find(kw)
                    if idx != -1:
                        msg_start = idx + len(kw)
                        break

                if msg_start == -1:
                    for skip in (
                        f"whatsapp {recipient} ",
                        f"{recipient} on whatsapp ",
                    ):
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
                "Ð·Ð°Ð²ÐµÐ´Ð¸ Ð¼Ðµ Ð´Ð¾ ",
                "Ð·Ð°ÐºÐ°Ñ€Ð°Ð¹ Ð¼Ðµ Ð´Ð¾ ",
                "Ð¾Ñ‚Ð²ÐµÐ´Ð¸ Ð¼Ðµ Ð´Ð¾ ",
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

            match = re.search(
                r"(https?://\S+|www\.\S+|\S+\.(?:com|org|net|io|co\.uk|dev))",
                lower,
            )
            if match:
                entities["url"] = match.group(1)
        if goal_type == "draft_email":
            import re

            match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", lower)
            if match:
                entities["recipient"] = match.group(0)
            for kw in ("saying ", "that ", "with body ", "body: "):
                idx = lower.find(kw)
                if idx != -1:
                    entities["body"] = lower[idx + len(kw):].strip()
                    break
        return entities

    @staticmethod
    def _build_goal(
        goal_type: str,
        app_package: str,
        entities: dict,
        transcript: str,
    ) -> str:
        if goal_type == "send_message":
            recipient = entities.get("recipient", "contact")
            msg = entities.get("message", "")
            preview = f": '{msg[:40]}'" if msg else ""
            return f"Send WhatsApp message to {recipient}{preview}"
        if goal_type in ("navigate_to", "start_navigation"):
            destination = entities.get("destination", "destination")
            return f"Navigate to {destination}"
        if goal_type == "search":
            query = entities.get("query", "")
            return f"Search for '{query}'"
        if goal_type == "scroll_view":
            direction = entities.get("direction", "down")
            return f"Scroll {direction} in the current view"
        if goal_type == "browse_current_view":
            return "Browse items in the current view"
        if goal_type == "continue_in_app":
            return "Continue in the current app"
        if goal_type == "draft_email":
            return "Draft email"
        if goal_type == "invalid_request":
            return "No actionable phone command"
        return transcript[:100]


def _missing_fields_for_intent(result: IntentResult) -> list[str]:
    missing: list[str] = []
    entities = result.entities or {}
    goal_type = str(result.goal_type or "").strip()

    if goal_type == "invalid_request":
        return ["target_app", "goal"]

    if not str(result.app_package or "").strip():
        missing.append("target_app")

    required_entities: dict[str, list[str]] = {
        "send_message": ["recipient", "message"],
        "navigate_to": ["destination"],
        "start_navigation": ["destination"],
        "search": ["query"],
        "open_website": ["url"],
        "draft_email": ["recipient", "body"],
    }
    for field_name in required_entities.get(goal_type, []):
        if not str(entities.get(field_name) or "").strip():
            missing.append(field_name)

    if not str(result.goal or "").strip():
        missing.append("goal")

    deduped: list[str] = []
    for field_name in missing:
        if field_name not in deduped:
            deduped.append(field_name)
    return deduped


def _normalize_context(context: dict | None) -> dict:
    if not isinstance(context, dict):
        return {}
    normalized = {
        "follow_up_mode": bool(context.get("follow_up_mode")),
        "current_app_package": str(context.get("current_app_package") or "").strip(),
        "current_app_name": str(context.get("current_app_name") or "").strip(),
        "current_window_title": str(context.get("current_window_title") or "").strip(),
        "previous_goal": str(context.get("previous_goal") or "").strip(),
        "previous_entities": context.get("previous_entities")
        if isinstance(context.get("previous_entities"), dict)
        else {},
    }
    if not normalized["current_app_package"] and isinstance(context.get("previous_app_package"), str):
        normalized["current_app_package"] = context["previous_app_package"].strip()
    if not normalized["current_app_name"] and isinstance(context.get("previous_app_name"), str):
        normalized["current_app_name"] = context["previous_app_name"].strip()
    return {
        key: value
        for key, value in normalized.items()
        if value not in ("", {}, False)
    }


def _looks_like_followup_instruction(transcript: str) -> bool:
    lower = transcript.lower().strip()
    if not lower:
        return False
    followup_markers = (
        "scroll",
        "swipe",
        "open the first",
        "open first",
        "look at",
        "look through",
        "continue",
        "go back",
        "back",
        "search there",
        "search here",
        "type ",
        "write ",
        "tap ",
        "click ",
        "превърти",
        "скрол",
        "разгледай",
        "отвори първ",
        "продължи",
        "върни се",
        "назад",
        "търси там",
        "търси тук",
        "напиши",
        "натисни",
        "кликни",
    )
    return any(marker in lower for marker in followup_markers)


def _infer_followup_goal_type(transcript: str) -> str:
    lower = transcript.lower()
    if any(word in lower for word in ("scroll", "swipe", "превърти", "скрол", "надолу", "нагоре")):
        return "scroll_view"
    if any(word in lower for word in ("look at", "look through", "open the first", "резултат", "сайтов", "разгледай", "отвори")):
        return "browse_current_view"
    return "continue_in_app"


def _build_contextual_goal(transcript: str, goal_type: str) -> str:
    clean = " ".join(str(transcript or "").split()).strip()
    if goal_type == "scroll_view":
        return f"Scroll the current view: {clean}"[:200]
    if goal_type == "browse_current_view":
        return f"Browse items in the current view: {clean}"[:200]
    return f"Continue in the current app: {clean}"[:200]


def _merge_followup_entities(
    transcript: str,
    goal_type: str,
    context: dict | None,
) -> dict:
    entities = dict((context or {}).get("previous_entities") or {})
    lower = transcript.lower()
    if goal_type == "scroll_view":
        if any(word in lower for word in ("up", "нагоре")):
            entities["direction"] = "up"
        else:
            entities["direction"] = "down"
    if any(word in lower for word in ("first", "първ")):
        entities["ordinal"] = "first"
    return entities


def _risk_level_for_goal_type(goal_type: str) -> str:
    if goal_type in {"send_message", "draft_email"}:
        return "high"
    if goal_type in {"navigate_to", "start_navigation", "search", "open_website", "browse_current_view"}:
        return "medium"
    return "low"


def _is_ambiguous_intent(result: IntentResult) -> bool:
    ambiguity_flags = [
        str(flag).strip().lower() for flag in (result.ambiguity_flags or [])
    ]
    if result.goal_type == "invalid_request":
        return True
    if result.confidence < 0.55:
        return True
    return any(
        "ambiguous" in flag
        or "unknown" in flag
        or "not_actionable" in flag
        or "unclear" in flag
        for flag in ambiguity_flags
    )


def _clarification_question_for_intent(result: IntentResult) -> str:
    missing = _missing_fields_for_intent(result)
    goal_type = str(result.goal_type or "").strip()
    app_name = str(result.target_app or "").strip()

    if goal_type == "invalid_request":
        return "Кое приложение да използвам и какво точно да направя там?"

    if goal_type == "send_message":
        if "recipient" in missing and "message" in missing:
            return "На кого да пиша и какво да кажа?"
        if "recipient" in missing:
            return "На кого да изпратя съобщението?"
        if "message" in missing:
            return "Какво да пише в съобщението?"

    if goal_type in ("navigate_to", "start_navigation") and "destination" in missing:
        return "Накъде да навигирам?"

    if goal_type == "search" and "query" in missing:
        if app_name:
            return f"Какво да потърся в {app_name}?"
        return "Какво да потърся?"

    if goal_type == "open_website" and "url" in missing:
        return "Кой сайт да отворя?"

    if goal_type == "draft_email":
        if "recipient" in missing and "body" in missing:
            return "До кого да е имейлът и какво да пише в него?"
        if "recipient" in missing:
            return "До кого да е имейлът?"
        if "body" in missing:
            return "Какво да пише в имейла?"

    if goal_type == "open_app" and "target_app" in missing:
        return "Кое приложение да отворя?"

    if "target_app" in missing:
        return "Кое приложение да използвам за това?"

    if _is_ambiguous_intent(result):
        if app_name:
            return f"Какво точно да направя в {app_name}?"
        return "Какво точно да направя на телефона?"

    return ""

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
            return "com.google.android.apps.maps", "Google Maps", "navigate_to", "medium"
        if "brawl stars" in lower or "brawlstars" in lower or "brawl star" in lower:
            return "com.supercell.brawlstars", "Brawl Stars", "open_app", "low"
        if any(w in lower for w in ("search", "google", "look up", "find")):
            return "com.android.chrome", "Chrome", "search", "low"
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
            return f"Send WhatsApp message to {recipient}{preview}"
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
