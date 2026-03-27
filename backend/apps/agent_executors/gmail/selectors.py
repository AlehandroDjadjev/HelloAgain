"""Versioned selector registry for Gmail (com.google.android.gm)."""
from __future__ import annotations
from apps.agent_executors.whatsapp.selectors import _pick_version, _substitute_params

GMAIL_SELECTORS: dict[str, dict[str, list[dict]]] = {

    "compose_button": {
        "default": [
            {"content_desc_contains": "Compose"},
            {"view_id": "com.google.android.gm:id/compose_button"},
            {"text_contains": "Compose"},
        ],
    },

    "to_field": {
        "default": [
            {"content_desc_contains": "To"},
            {"view_id": "com.google.android.gm:id/to"},
            {"class_name": "android.widget.MultiAutoCompleteTextView"},
        ],
    },

    "subject_field": {
        "default": [
            {"content_desc_contains": "Subject"},
            {"view_id": "com.google.android.gm:id/subject"},
        ],
    },

    "body_field": {
        "default": [
            {"content_desc_contains": "Compose email"},
            {"view_id": "com.google.android.gm:id/body"},
            {"class_name": "android.widget.EditText", "enabled": True},
        ],
    },

    "send_button": {
        "default": [
            {"content_desc_contains": "Send"},
            {"view_id": "com.google.android.gm:id/send"},
        ],
    },

    "search_button": {
        "default": [
            {"content_desc_contains": "Search in mail"},
            {"content_desc_contains": "Search"},
            {"view_id": "com.google.android.gm:id/search_button"},
        ],
    },

    "back_button": {
        "default": [
            {"content_desc_contains": "Navigate up"},
            {"content_desc_contains": "Back"},
        ],
    },

    "discard_draft_button": {
        "default": [
            {"content_desc_contains": "Discard draft"},
            {"text_contains": "Discard"},
        ],
    },
}


def get_selectors(
    element_name: str,
    selector_params: dict | None = None,
    app_version: str | None = None,
) -> list[dict]:
    entry = GMAIL_SELECTORS.get(element_name)
    if entry is None:
        return []
    candidates = _pick_version(entry, app_version)
    if selector_params:
        candidates = _substitute_params(candidates, selector_params)
    return candidates


def all_element_names() -> list[str]:
    return list(GMAIL_SELECTORS.keys())
