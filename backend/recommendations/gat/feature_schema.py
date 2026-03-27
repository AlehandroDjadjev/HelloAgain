"""
feature_schema.py
-----------------
Base feature definitions plus a lightweight runtime registry for custom
experiment features. Custom features are stored in a local JSON file so the
tester can add and remove dimensions without requiring DB migrations.
"""

import json
import re
from pathlib import Path


_BASE_FEATURE_DEFINITIONS: list[dict[str, str]] = [
    {"name": "extroversion", "label": "Social Energy", "group": "Personality & Mood", "priority": "core"},
    {"name": "openness", "label": "Openness", "group": "Personality & Mood", "priority": "secondary"},
    {"name": "agreeableness", "label": "Agreeableness", "group": "Personality & Mood", "priority": "core"},
    {"name": "emotional_warmth", "label": "Warmth", "group": "Personality & Mood", "priority": "core"},
    {"name": "humor", "label": "Humor", "group": "Personality & Mood", "priority": "core"},
    {"name": "positivity", "label": "Positivity", "group": "Personality & Mood", "priority": "core"},
    {"name": "patience", "label": "Patience", "group": "Personality & Mood", "priority": "core"},
    {"name": "empathy", "label": "Empathy", "group": "Personality & Mood", "priority": "core"},
    {"name": "reliability", "label": "Reliability", "group": "Personality & Mood", "priority": "core"},
    {"name": "independence", "label": "Independence", "group": "Personality & Mood", "priority": "secondary"},
    {"name": "adaptability", "label": "Adaptability", "group": "Personality & Mood", "priority": "core"},
    {"name": "emotional_stability", "label": "Emotional Stability", "group": "Personality & Mood", "priority": "core"},
    {"name": "assertiveness", "label": "Assertiveness", "group": "Personality & Mood", "priority": "secondary"},
    {"name": "playfulness", "label": "Playfulness", "group": "Personality & Mood", "priority": "secondary"},
    {"name": "curiosity", "label": "Curiosity", "group": "Personality & Mood", "priority": "secondary"},
    {"name": "routine_orientation", "label": "Routine Orientation", "group": "Personality & Mood", "priority": "secondary"},
    {"name": "interest_music", "label": "Music", "group": "Interests & Activities", "priority": "secondary"},
    {"name": "interest_nature", "label": "Nature", "group": "Interests & Activities", "priority": "secondary"},
    {"name": "interest_family", "label": "Family Centrality", "group": "Interests & Activities", "priority": "core"},
    {"name": "interest_cooking", "label": "Cooking", "group": "Interests & Activities", "priority": "secondary"},
    {"name": "interest_religion", "label": "Faith Interest", "group": "Interests & Activities", "priority": "core"},
    {"name": "interest_history", "label": "History", "group": "Interests & Activities", "priority": "secondary"},
    {"name": "interest_travel", "label": "Travel", "group": "Interests & Activities", "priority": "secondary"},
    {"name": "interest_arts", "label": "Arts", "group": "Interests & Activities", "priority": "secondary"},
    {"name": "interest_books", "label": "Books", "group": "Interests & Activities", "priority": "secondary"},
    {"name": "interest_sports", "label": "Sports", "group": "Interests & Activities", "priority": "secondary"},
    {"name": "interest_games", "label": "Games", "group": "Interests & Activities", "priority": "secondary"},
    {"name": "interest_crafts", "label": "Crafts", "group": "Interests & Activities", "priority": "secondary"},
    {"name": "interest_movies", "label": "Movies", "group": "Interests & Activities", "priority": "secondary"},
    {"name": "interest_pets", "label": "Pets", "group": "Interests & Activities", "priority": "secondary"},
    {"name": "interest_volunteering", "label": "Volunteering", "group": "Interests & Activities", "priority": "secondary"},
    {"name": "interest_technology", "label": "Technology", "group": "Interests & Activities", "priority": "secondary"},
    {"name": "verbosity", "label": "Talkativeness", "group": "Communication", "priority": "core"},
    {"name": "formality", "label": "Formality", "group": "Communication", "priority": "secondary"},
    {"name": "story_telling", "label": "Storytelling", "group": "Communication", "priority": "core"},
    {"name": "directness", "label": "Directness", "group": "Communication", "priority": "secondary"},
    {"name": "listening_style", "label": "Listening", "group": "Communication", "priority": "core"},
    {"name": "question_asking", "label": "Question Asking", "group": "Communication", "priority": "secondary"},
    {"name": "conversation_depth", "label": "Conversation Depth", "group": "Communication", "priority": "core"},
    {"name": "conflict_tact", "label": "Conflict Tact", "group": "Communication", "priority": "secondary"},
    {"name": "responsiveness", "label": "Responsiveness", "group": "Communication", "priority": "core"},
    {"name": "reassurance_expression", "label": "Reassurance", "group": "Communication", "priority": "secondary"},
    {"name": "pace_of_speech", "label": "Speech Pace", "group": "Communication", "priority": "secondary"},
    {"name": "prefers_small_groups", "label": "Small Group Preference", "group": "Social Preferences", "priority": "core"},
    {"name": "activity_level", "label": "Activity Level", "group": "Social Preferences", "priority": "core"},
    {"name": "nostalgia_index", "label": "Nostalgia", "group": "Social Preferences", "priority": "core"},
    {"name": "spiritual_alignment", "label": "Spiritual Alignment", "group": "Social Preferences", "priority": "core"},
    {"name": "prefers_structure", "label": "Structure Preference", "group": "Social Preferences", "priority": "secondary"},
    {"name": "community_involvement", "label": "Community Involvement", "group": "Social Preferences", "priority": "core"},
    {"name": "hosting_preference", "label": "Hosting Preference", "group": "Social Preferences", "priority": "secondary"},
    {"name": "receiving_support_comfort", "label": "Comfort Receiving Support", "group": "Social Preferences", "priority": "secondary"},
    {"name": "needs_personal_space", "label": "Needs Personal Space", "group": "Social Preferences", "priority": "core"},
    {"name": "schedule_flexibility", "label": "Schedule Flexibility", "group": "Social Preferences", "priority": "secondary"},
    {"name": "tolerance_for_noise", "label": "Noise Tolerance", "group": "Social Preferences", "priority": "secondary"},
    {"name": "mobility_confidence", "label": "Mobility Confidence", "group": "Support & Lifestyle", "priority": "core"},
    {"name": "self_sufficiency", "label": "Self Sufficiency", "group": "Support & Lifestyle", "priority": "core"},
    {"name": "practical_help_orientation", "label": "Practical Help", "group": "Support & Lifestyle", "priority": "secondary"},
    {"name": "caretaking_drive", "label": "Caretaking Drive", "group": "Support & Lifestyle", "priority": "secondary"},
    {"name": "health_caution", "label": "Health Caution", "group": "Support & Lifestyle", "priority": "secondary"},
    {"name": "adventure_comfort", "label": "Adventure Comfort", "group": "Support & Lifestyle", "priority": "secondary"},
    {"name": "change_tolerance", "label": "Change Tolerance", "group": "Support & Lifestyle", "priority": "secondary"},
    {"name": "financial_caution", "label": "Financial Caution", "group": "Support & Lifestyle", "priority": "secondary"},
    {"name": "local_attachment", "label": "Local Attachment", "group": "Support & Lifestyle", "priority": "secondary"},
    {"name": "pet_attachment", "label": "Pet Attachment", "group": "Support & Lifestyle", "priority": "secondary"},
]

FEATURE_NAMES: list[str] = [item["name"] for item in _BASE_FEATURE_DEFINITIONS]
FEATURE_DIM: int = len(FEATURE_NAMES)

FEATURE_GROUPS: dict[str, list[str]] = {}
for item in _BASE_FEATURE_DEFINITIONS:
    FEATURE_GROUPS.setdefault(item["group"], []).append(item["name"])

DEFAULT_FEATURE_VECTOR: dict[str, float] = {name: 0.5 for name in FEATURE_NAMES}
_FEATURE_METADATA: dict[str, dict] = {
    item["name"]: {
        "label": item["label"],
        "group": item["group"],
        "priority": item["priority"],
    }
    for item in _BASE_FEATURE_DEFINITIONS
}

_CUSTOM_FEATURES_PATH = Path(__file__).resolve().parent / "custom_features.json"
_NAME_PATTERN = re.compile(r"[^a-z0-9_]+")


def _title_from_name(name: str) -> str:
    return " ".join(part.capitalize() for part in name.split("_") if part)


def normalize_feature_name(name: str) -> str:
    normalized = _NAME_PATTERN.sub("_", name.strip().lower())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized


def _read_custom_feature_file() -> list[dict]:
    if not _CUSTOM_FEATURES_PATH.exists():
        return []
    try:
        return json.loads(_CUSTOM_FEATURES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _write_custom_feature_file(items: list[dict]) -> None:
    _CUSTOM_FEATURES_PATH.write_text(json.dumps(items, indent=2), encoding="utf-8")


def get_custom_features() -> list[dict]:
    items = []
    seen_names: set[str] = set()
    for raw_item in _read_custom_feature_file():
        name = normalize_feature_name(str(raw_item.get("name", "")))
        if not name or name in seen_names or name in FEATURE_NAMES:
            continue
        seen_names.add(name)
        items.append(
            {
                "name": name,
                "label": str(raw_item.get("label") or _title_from_name(name)),
                "group": str(raw_item.get("group") or "Custom"),
                "default": float(raw_item.get("default", 0.5)),
                "is_custom": True,
                "priority": "custom",
                "is_recommended_core": False,
            }
        )
    return items


def get_feature_names() -> list[str]:
    return FEATURE_NAMES + [item["name"] for item in get_custom_features()]


def get_feature_groups() -> dict[str, list[str]]:
    groups = {name: list(features) for name, features in FEATURE_GROUPS.items()}
    for item in get_custom_features():
        groups.setdefault(item["group"], []).append(item["name"])
    return groups


def get_recommended_core_features() -> list[str]:
    return [
        name
        for name in FEATURE_NAMES
        if _FEATURE_METADATA.get(name, {}).get("priority") == "core"
    ]


def get_secondary_features() -> list[str]:
    return [
        name
        for name in FEATURE_NAMES
        if _FEATURE_METADATA.get(name, {}).get("priority") == "secondary"
    ]


def get_default_feature_vector() -> dict[str, float]:
    values = dict(DEFAULT_FEATURE_VECTOR)
    for item in get_custom_features():
        values[item["name"]] = float(max(0.0, min(1.0, item["default"])))
    return values


def get_feature_details() -> list[dict]:
    details = []
    groups = get_feature_groups()
    feature_to_group = {
        feature_name: group_name
        for group_name, feature_names in groups.items()
        for feature_name in feature_names
    }
    for feature_name in FEATURE_NAMES:
        metadata = _FEATURE_METADATA.get(feature_name, {})
        details.append(
            {
                "name": feature_name,
                "label": metadata.get("label", _title_from_name(feature_name)),
                "group": metadata.get("group", feature_to_group.get(feature_name, "Base")),
                "default": 0.5,
                "is_custom": False,
                "priority": metadata.get("priority", "secondary"),
                "is_recommended_core": metadata.get("priority") == "core",
            }
        )
    details.extend(get_custom_features())
    return details


def is_known_feature(name: str) -> bool:
    return normalize_feature_name(name) in set(get_feature_names())


def add_custom_feature(
    name: str,
    label: str | None = None,
    group: str | None = None,
    default: float = 0.5,
) -> dict:
    normalized = normalize_feature_name(name)
    if not normalized:
        raise ValueError("Feature name cannot be empty.")
    if normalized in FEATURE_NAMES:
        raise ValueError("That feature already exists as a built-in feature.")

    items = get_custom_features()
    if any(item["name"] == normalized for item in items):
        raise ValueError("That custom feature already exists.")

    item = {
        "name": normalized,
        "label": (label or _title_from_name(normalized)).strip() or _title_from_name(normalized),
        "group": (group or "Custom").strip() or "Custom",
        "default": float(max(0.0, min(1.0, default))),
        "is_custom": True,
    }
    items.append(item)
    _write_custom_feature_file(items)
    return item


def remove_custom_feature(name: str) -> bool:
    normalized = normalize_feature_name(name)
    items = get_custom_features()
    filtered = [item for item in items if item["name"] != normalized]
    if len(filtered) == len(items):
        return False
    if filtered:
        _write_custom_feature_file(filtered)
    elif _CUSTOM_FEATURES_PATH.exists():
        _CUSTOM_FEATURES_PATH.unlink()
    return True


def reset_custom_features() -> None:
    if _CUSTOM_FEATURES_PATH.exists():
        _CUSTOM_FEATURES_PATH.unlink()


def vector_to_list(feature_dict: dict, feature_names: list[str] | None = None) -> list[float]:
    names = feature_names or get_feature_names()
    defaults = get_default_feature_vector()
    return [float(feature_dict.get(name, defaults.get(name, 0.5))) for name in names]


def list_to_vector(feature_list: list, feature_names: list[str] | None = None) -> dict[str, float]:
    names = feature_names or get_feature_names()
    if len(feature_list) != len(names):
        raise ValueError(f"Expected {len(names)} features, got {len(feature_list)}")
    return {name: float(feature_list[i]) for i, name in enumerate(names)}
