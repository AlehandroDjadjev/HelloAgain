"""
WhatsApp versioned selector registry.

Each top-level key is a logical element name.
Under it, version-string keys map to ordered selector lists to try.
The special key "default" is used when no version matches.

Version matching rules (applied in this order):
  1. Exact match:     "2.24.3.78" → {"2.24.3.78": [...]}
  2. Major.minor:     "2.24.3.78" → {"2.24": [...]}
  3. Semver prefix "X.Y+": any version >= X.Y uses this entry
  4. Fallback:        "default"

Selector dict keys match the snake_case Selector schema used by Android:
  text              – exact text match
  text_contains     – case-insensitive substring of node text
  content_desc      – exact contentDescription
  content_desc_contains – case-insensitive substring of contentDescription
  view_id           – resource ID string (e.g. "com.whatsapp:id/entry")
  class_name        – Android class (e.g. "android.widget.EditText")
  clickable         – bool
  enabled           – bool
  focused           – bool
  index_in_parent   – int

Parameterised values use Python str.format_map() syntax, e.g.
  {"text_contains": "{contact_name}"}
These are resolved at execution time by calling get_selectors() with
selector_params={"contact_name": "Alex"}.
"""
from __future__ import annotations

import re

# ── Registry ──────────────────────────────────────────────────────────────────

WHATSAPP_SELECTORS: dict[str, dict[str, list[dict]]] = {

    # ── Main screen navigation ────────────────────────────────────────────────

    "search_button": {
        "default": [
            {"content_desc_contains": "Search"},
            {"view_id": "com.whatsapp:id/menuitem_search"},
            {"text_contains": "Search", "clickable": True},
        ],
        "2.24+": [
            {"content_desc_contains": "Search"},
            {"view_id": "com.whatsapp:id/home_toolbar_search_btn"},
            {"content_desc": "Search"},
        ],
    },

    "new_chat_button": {
        "default": [
            {"content_desc_contains": "New chat"},
            {"view_id": "com.whatsapp:id/fab"},
            {"content_desc": "New chat"},
        ],
    },

    # ── Search flow ───────────────────────────────────────────────────────────

    "search_input": {
        "default": [
            {"class_name": "android.widget.EditText", "focused": True},
            {"view_id": "com.whatsapp:id/search_input"},
            {"class_name": "android.widget.EditText", "enabled": True},
        ],
    },

    "contact_item": {
        "default": [
            # Resolved at runtime via selector_params={"contact_name": "..."}
            {"text_contains": "{contact_name}"},
            {"content_desc_contains": "{contact_name}"},
        ],
    },

    # ── Chat thread ───────────────────────────────────────────────────────────

    "message_input": {
        "default": [
            {
                "class_name": "android.widget.EditText",
                "content_desc_contains": "Message",
                "enabled": True,
            },
            {
                "class_name": "android.widget.EditText",
                "enabled": True,
            },
            {"view_id": "com.whatsapp:id/entry"},
        ],
        "2.23+": [
            {"content_desc_contains": "Message", "enabled": True},
            {"view_id": "com.whatsapp:id/entry"},
            {"class_name": "android.widget.EditText", "enabled": True},
        ],
    },

    "send_button": {
        "default": [
            {"content_desc_contains": "Send"},
            {"view_id": "com.whatsapp:id/send"},
            {"content_desc": "Send"},
        ],
    },

    "attach_button": {
        "default": [
            {"content_desc_contains": "Attach"},
            {"view_id": "com.whatsapp:id/input_attach_button"},
        ],
    },

    "voice_note_button": {
        "default": [
            {"content_desc_contains": "Voice message"},
            {"view_id": "com.whatsapp:id/audio_rec_slide_text"},
        ],
    },

    # ── Back / navigation ─────────────────────────────────────────────────────

    "back_button": {
        "default": [
            {"content_desc_contains": "Navigate up"},
            {"content_desc_contains": "Back"},
        ],
    },
}


# ── Public API ────────────────────────────────────────────────────────────────

def get_selectors(
    element_name: str,
    selector_params: dict | None = None,
    app_version: str | None = None,
) -> list[dict]:
    """
    Return the ordered selector list for *element_name*.

    Version resolution order:
      1. Exact version string (e.g. "2.24.3.78")
      2. "X.Y+" range match (highest matching range wins)
      3. "default"

    If *selector_params* is provided, placeholder values ({key}) in selector
    string fields are substituted via str.format_map().

    Returns an empty list if *element_name* is not in the registry.
    """
    entry = WHATSAPP_SELECTORS.get(element_name)
    if entry is None:
        return []

    candidates = _pick_version(entry, app_version)
    if selector_params:
        candidates = _substitute_params(candidates, selector_params)
    return candidates


def all_element_names() -> list[str]:
    return list(WHATSAPP_SELECTORS.keys())


# ── Version-matching helpers ──────────────────────────────────────────────────

_RANGE_RE = re.compile(r"^(\d+\.\d+)\+$")  # e.g. "2.24+"


def _pick_version(entry: dict[str, list[dict]], app_version: str | None) -> list[dict]:
    if app_version:
        # 1. Exact match
        if app_version in entry:
            return list(entry[app_version])

        # 2. "X.Y+" range — collect all matching ranges, pick the highest
        matching_ranges: list[tuple[tuple[int, int], list[dict]]] = []
        for key, selectors in entry.items():
            m = _RANGE_RE.match(key)
            if not m:
                continue
            threshold_str = m.group(1)
            try:
                threshold = tuple(int(p) for p in threshold_str.split("."))  # type: ignore[assignment]
                version_tuple = tuple(int(p) for p in app_version.split(".")[:2])  # type: ignore[assignment]
                if version_tuple >= threshold:
                    matching_ranges.append((threshold, selectors))  # type: ignore[arg-type]
            except ValueError:
                continue

        if matching_ranges:
            # Highest threshold wins
            _, best = max(matching_ranges, key=lambda t: t[0])
            return list(best)

    return list(entry.get("default", []))


def _substitute_params(candidates: list[dict], params: dict) -> list[dict]:
    """Replace {placeholder} values in selector dicts with runtime values."""
    result: list[dict] = []
    for candidate in candidates:
        substituted: dict = {}
        for k, v in candidate.items():
            if isinstance(v, str) and "{" in v:
                try:
                    v = v.format_map(params)
                except (KeyError, ValueError):
                    pass  # leave template string as-is if key is missing
            substituted[k] = v
        result.append(substituted)
    return result
