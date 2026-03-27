"""Versioned selector registry for Google Maps (com.google.android.apps.maps)."""
from __future__ import annotations
from apps.agent_executors.whatsapp.selectors import _pick_version, _substitute_params

MAPS_SELECTORS: dict[str, dict[str, list[dict]]] = {

    "search_input": {
        "default": [
            {"content_desc_contains": "Search here"},
            {"content_desc_contains": "Search"},
            {"view_id": "com.google.android.apps.maps:id/search_omnibox_text_box"},
            {"class_name": "android.widget.EditText", "enabled": True},
        ],
    },

    "destination_result": {
        "default": [
            # {destination} substituted at runtime
            {"text_contains": "{destination}"},
            {"content_desc_contains": "{destination}"},
        ],
    },

    "directions_button": {
        "default": [
            {"content_desc_contains": "Directions"},
            {"view_id": "com.google.android.apps.maps:id/directions_fab"},
            {"text_contains": "Directions"},
        ],
    },

    "start_navigation_button": {
        "default": [
            {"content_desc_contains": "Start"},
            {"text_contains": "Start"},
            {"view_id": "com.google.android.apps.maps:id/start_button"},
        ],
    },

    "back_button": {
        "default": [
            {"content_desc_contains": "Navigate up"},
            {"content_desc_contains": "Back"},
        ],
    },

    "clear_search": {
        "default": [
            {"content_desc_contains": "Clear"},
            {"view_id": "com.google.android.apps.maps:id/clear_button"},
        ],
    },
}


def get_selectors(
    element_name: str,
    selector_params: dict | None = None,
    app_version: str | None = None,
) -> list[dict]:
    entry = MAPS_SELECTORS.get(element_name)
    if entry is None:
        return []
    candidates = _pick_version(entry, app_version)
    if selector_params:
        candidates = _substitute_params(candidates, selector_params)
    return candidates


def all_element_names() -> list[str]:
    return list(MAPS_SELECTORS.keys())
