"""Versioned selector registry for Google Chrome (com.android.chrome)."""
from __future__ import annotations
from apps.agent_executors.whatsapp.selectors import _pick_version, _substitute_params

CHROME_SELECTORS: dict[str, dict[str, list[dict]]] = {

    "omnibox": {
        "default": [
            {"content_desc_contains": "Search or type URL"},
            {"content_desc_contains": "Address and search bar"},
            {"view_id": "com.android.chrome:id/url_bar"},
            {"class_name": "android.widget.EditText", "focused": True},
            {"class_name": "android.widget.EditText", "enabled": True},
        ],
        "120+": [
            {"content_desc_contains": "Search or type URL"},
            {"view_id": "com.android.chrome:id/url_bar"},
            {"class_name": "android.widget.EditText", "enabled": True},
        ],
    },

    "go_button": {
        "default": [
            {"content_desc_contains": "Go"},
            {"view_id": "com.android.chrome:id/url_action_button"},
            {"class_name": "android.widget.ImageView", "content_desc_contains": "Go"},
        ],
    },

    "back_button": {
        "default": [
            {"content_desc_contains": "Navigate up"},
            {"content_desc_contains": "Back"},
            {"view_id": "com.android.chrome:id/back_button"},
        ],
    },

    "forward_button": {
        "default": [
            {"content_desc_contains": "Forward"},
            {"view_id": "com.android.chrome:id/forward_button"},
        ],
    },

    "new_tab_button": {
        "default": [
            {"content_desc_contains": "New tab"},
            {"view_id": "com.android.chrome:id/new_tab_button"},
        ],
    },

    "tabs_switcher": {
        "default": [
            {"content_desc_contains": "Switch or close tabs"},
            {"view_id": "com.android.chrome:id/tab_switcher_button"},
        ],
    },

    "reload_button": {
        "default": [
            {"content_desc_contains": "Refresh"},
            {"view_id": "com.android.chrome:id/refresh_button"},
        ],
    },
}


def get_selectors(
    element_name: str,
    selector_params: dict | None = None,
    app_version: str | None = None,
) -> list[dict]:
    entry = CHROME_SELECTORS.get(element_name)
    if entry is None:
        return []
    candidates = _pick_version(entry, app_version)
    if selector_params:
        candidates = _substitute_params(candidates, selector_params)
    return candidates


def all_element_names() -> list[str]:
    return list(CHROME_SELECTORS.keys())
