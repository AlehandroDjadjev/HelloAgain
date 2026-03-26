from __future__ import annotations

import re
from typing import Any

from recommendations.gat.feature_schema import get_recommended_core_features

_TOKEN_RE = re.compile(r"[a-zA-Z']+")
_GENERIC_ANSWERS = {
    "dont know",
    "don't know",
    "not sure",
    "unsure",
    "maybe",
    "hard to say",
    "unknown",
}

_QUESTION_BANK: list[dict[str, Any]] = [
    {
        "id": "preferred_company",
        "prompt": "What kind of company feels most natural for this person?",
        "placeholder": "Example: She does best one-to-one over tea and gets drained by noisy groups.",
        "help_text": "Mention whether they prefer lively groups, one-to-one time, or a balanced mix.",
        "feature_names": ["extroversion", "prefers_small_groups", "verbosity"],
        "profiles": [
            {
                "id": "lively_groups",
                "phrases": (
                    "lively group",
                    "big group",
                    "busy room",
                    "crowd",
                    "social setting",
                    "around people",
                    "group conversation",
                ),
                "signals": {"extroversion": 0.84, "prefers_small_groups": 0.22, "verbosity": 0.72},
                "confidence": 0.84,
            },
            {
                "id": "one_to_one",
                "phrases": (
                    "one on one",
                    "one-to-one",
                    "one to one",
                    "small group",
                    "quiet visit",
                    "calm company",
                    "just one person",
                ),
                "signals": {"extroversion": 0.34, "prefers_small_groups": 0.86, "verbosity": 0.44},
                "confidence": 0.85,
            },
            {
                "id": "balanced",
                "phrases": ("both", "depends", "mix", "balanced", "either works", "comfortable either way"),
                "signals": {"extroversion": 0.56, "prefers_small_groups": 0.52, "verbosity": 0.56},
                "confidence": 0.78,
            },
        ],
    },
    {
        "id": "conversation_style",
        "prompt": "How does this person usually come across in conversation?",
        "placeholder": "Example: He is direct and practical, not very chatty, but he says exactly what he means.",
        "help_text": "Describe whether they are more talkative, story-driven, calm, listening-focused, or direct.",
        "feature_names": ["verbosity", "story_telling", "patience", "directness", "listening_style"],
        "profiles": [
            {
                "id": "talkative_storyteller",
                "phrases": (
                    "talkative",
                    "chatty",
                    "story",
                    "storyteller",
                    "loves talking",
                    "keeps conversation going",
                    "shares memories",
                ),
                "signals": {"verbosity": 0.82, "story_telling": 0.86, "patience": 0.58, "listening_style": 0.58},
                "confidence": 0.85,
            },
            {
                "id": "calm_listener",
                "phrases": (
                    "good listener",
                    "listens",
                    "quiet",
                    "thoughtful",
                    "patient",
                    "calm",
                    "speaks after listening",
                ),
                "signals": {"verbosity": 0.36, "story_telling": 0.48, "patience": 0.82, "listening_style": 0.84},
                "confidence": 0.85,
            },
            {
                "id": "direct_practical",
                "phrases": (
                    "direct",
                    "straight to the point",
                    "to the point",
                    "practical",
                    "plain spoken",
                    "straightforward",
                    "says what he means",
                    "says what she means",
                ),
                "signals": {"verbosity": 0.48, "story_telling": 0.34, "patience": 0.58, "directness": 0.84},
                "confidence": 0.83,
            },
        ],
    },
    {
        "id": "shared_time",
        "prompt": "What kind of time together would fit this person best?",
        "placeholder": "Example: He would rather go for a walk or run errands together than sit for a very long home visit.",
        "help_text": "Mention whether active outings, home visits, arts and music, faith and community, or a mix fits best.",
        "feature_names": ["activity_level", "interest_music", "interest_arts", "interest_religion", "interest_nature", "interest_family"],
        "profiles": [
            {
                "id": "active_outings",
                "phrases": (
                    "walk",
                    "walking",
                    "outings",
                    "errands",
                    "market",
                    "outside",
                    "active",
                    "garden",
                    "moving around",
                ),
                "signals": {"activity_level": 0.82, "interest_nature": 0.74},
                "confidence": 0.84,
            },
            {
                "id": "home_visits",
                "phrases": (
                    "home visit",
                    "at home",
                    "tea at home",
                    "quiet visit",
                    "familiar setting",
                    "stays home",
                    "home company",
                ),
                "signals": {"activity_level": 0.34, "prefers_small_groups": 0.76, "interest_family": 0.68},
                "confidence": 0.84,
            },
            {
                "id": "arts_music",
                "phrases": (
                    "music",
                    "songs",
                    "concert",
                    "crafts",
                    "art",
                    "theatre",
                    "cinema",
                    "performance",
                ),
                "signals": {"interest_music": 0.82, "interest_arts": 0.8},
                "confidence": 0.84,
            },
            {
                "id": "faith_community",
                "phrases": (
                    "church",
                    "faith",
                    "prayer",
                    "community group",
                    "choir",
                    "service",
                    "religious",
                    "fellowship",
                ),
                "signals": {"interest_religion": 0.82, "spiritual_alignment": 0.82, "community_involvement": 0.76},
                "confidence": 0.84,
            },
            {
                "id": "mixed",
                "phrases": ("mix", "varies", "depends", "different things", "open to both"),
                "signals": {"activity_level": 0.54},
                "confidence": 0.74,
            },
        ],
    },
    {
        "id": "pace_and_routine",
        "prompt": "What pace of life sounds most accurate for this person?",
        "placeholder": "Example: She likes a steady routine and does not enjoy a rushed or chaotic day.",
        "help_text": "Describe whether they seem active, steady and routine-based, or more low-key and slow-paced.",
        "feature_names": ["activity_level", "patience", "openness", "routine_orientation"],
        "profiles": [
            {
                "id": "active",
                "phrases": ("energetic", "busy", "keeps moving", "active", "likes staying busy", "on the go"),
                "signals": {"activity_level": 0.82, "openness": 0.64, "routine_orientation": 0.48},
                "confidence": 0.83,
            },
            {
                "id": "steady",
                "phrases": ("steady", "routine", "predictable", "regular rhythm", "balanced pace", "dependable rhythm"),
                "signals": {"activity_level": 0.56, "patience": 0.74, "routine_orientation": 0.82},
                "confidence": 0.84,
            },
            {
                "id": "low_key",
                "phrases": ("low key", "slow pace", "quiet pace", "takes it slow", "rests more", "not much bustle"),
                "signals": {"activity_level": 0.32, "patience": 0.78, "routine_orientation": 0.7},
                "confidence": 0.83,
            },
        ],
    },
    {
        "id": "closeness_style",
        "prompt": "How do they usually show closeness or emotional connection?",
        "placeholder": "Example: He is caring and dependable, but a bit formal, so his warmth shows more through actions than big emotions.",
        "help_text": "Mention whether they feel warm and caretaking, independent but friendly, or more reserved and formal.",
        "feature_names": ["emotional_warmth", "empathy", "agreeableness", "formality"],
        "profiles": [
            {
                "id": "warm_caretaking",
                "phrases": (
                    "warm",
                    "caring",
                    "takes care",
                    "affectionate",
                    "supportive",
                    "gentle",
                    "looks after people",
                ),
                "signals": {"emotional_warmth": 0.84, "empathy": 0.82, "agreeableness": 0.78, "formality": 0.46},
                "confidence": 0.85,
            },
            {
                "id": "independent_light",
                "phrases": (
                    "independent",
                    "light company",
                    "not too intense",
                    "likes space",
                    "companionship without pressure",
                    "friendly but independent",
                ),
                "signals": {"emotional_warmth": 0.56, "empathy": 0.54, "agreeableness": 0.6, "formality": 0.44},
                "confidence": 0.8,
            },
            {
                "id": "respectful_formal",
                "phrases": (
                    "formal",
                    "proper",
                    "respectful",
                    "reserved",
                    "polite",
                    "kind but reserved",
                    "keeps it proper",
                ),
                "signals": {"emotional_warmth": 0.62, "empathy": 0.66, "agreeableness": 0.68, "formality": 0.78},
                "confidence": 0.84,
            },
        ],
    },
    {
        "id": "independence_and_help",
        "prompt": "How do they relate to independence and receiving help?",
        "placeholder": "Example: She likes doing most things herself, but she is fine with practical help when it is clearly useful.",
        "help_text": "Mention whether they are highly independent, balanced about support, or openly comfortable with care and practical help.",
        "feature_names": ["independence", "self_sufficiency", "receiving_support_comfort", "practical_help_orientation", "caretaking_drive"],
        "profiles": [
            {
                "id": "very_independent",
                "phrases": (
                    "very independent",
                    "handles things alone",
                    "does things herself",
                    "does things himself",
                    "likes doing things alone",
                    "doesn't want help",
                    "does not want help",
                ),
                "signals": {"independence": 0.84, "self_sufficiency": 0.82, "receiving_support_comfort": 0.34},
                "confidence": 0.84,
            },
            {
                "id": "balanced_support",
                "phrases": (
                    "open to help",
                    "balanced",
                    "if needed",
                    "practical help is fine",
                    "comfortable with support",
                    "prefers independence but",
                ),
                "signals": {"independence": 0.62, "self_sufficiency": 0.68, "receiving_support_comfort": 0.62},
                "confidence": 0.82,
            },
            {
                "id": "care_and_support",
                "phrases": (
                    "welcomes help",
                    "comfortable receiving help",
                    "needs support",
                    "likes caring for others",
                    "warm with care",
                    "practical support matters",
                ),
                "signals": {"receiving_support_comfort": 0.8, "practical_help_orientation": 0.8, "caretaking_drive": 0.76},
                "confidence": 0.84,
            },
        ],
    },
    {
        "id": "interest_focus",
        "prompt": "What topics or activities most clearly hold this person's attention?",
        "placeholder": "Example: He lights up around local history, old books, and detailed stories from the past.",
        "help_text": "Mention a few concrete interests such as books and history, sport and movement, or technology and media.",
        "feature_names": ["interest_books", "interest_history", "interest_technology", "interest_sports", "interest_movies", "conversation_depth"],
        "profiles": [
            {
                "id": "reading_history",
                "phrases": (
                    "books",
                    "reading",
                    "history",
                    "old stories",
                    "past",
                    "museum",
                    "local history",
                    "reflective topics",
                ),
                "signals": {"interest_books": 0.82, "interest_history": 0.8, "conversation_depth": 0.7},
                "confidence": 0.83,
            },
            {
                "id": "sports_and_action",
                "phrases": (
                    "sports",
                    "football",
                    "basketball",
                    "tennis",
                    "exercise",
                    "walking",
                    "training",
                    "match",
                    "active hobbies",
                ),
                "signals": {"interest_sports": 0.84, "activity_level": 0.76, "interest_movies": 0.58},
                "confidence": 0.83,
            },
            {
                "id": "technology_and_media",
                "phrases": (
                    "phone",
                    "computer",
                    "tablet",
                    "technology",
                    "internet",
                    "shows",
                    "media",
                    "devices",
                ),
                "signals": {"interest_technology": 0.82, "interest_movies": 0.72, "curiosity": 0.68},
                "confidence": 0.83,
            },
        ],
    },
]


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text)]


def _normalize_answer(text: str) -> str:
    return " ".join(_tokenize(text))


def _axis_confidence(feature_confidence: dict[str, float], feature_names: list[str]) -> float:
    if not feature_names:
        return 0.0
    return sum(float(feature_confidence.get(name, 0.0)) for name in feature_names) / len(feature_names)


def needs_clarification(feature_confidence: dict[str, float]) -> tuple[bool, float]:
    core_features = get_recommended_core_features()
    core_scores = sorted((float(feature_confidence.get(name, 0.0)) for name in core_features), reverse=True)
    strong_core = sum(1 for score in core_scores if score >= 0.6)
    top_focus = sum(core_scores[:4]) / min(4, len(core_scores)) if core_scores else 0.0
    sufficiency_score = round(
        min(1.0, (0.55 * min(1.0, strong_core / 3.0)) + (0.45 * top_focus)),
        4,
    )
    return strong_core < 2 or top_focus < 0.52, sufficiency_score


def select_clarification_questions(feature_confidence: dict[str, float], limit: int = 4) -> list[dict[str, Any]]:
    ranked = sorted(
        _QUESTION_BANK,
        key=lambda item: _axis_confidence(feature_confidence, item["feature_names"]),
    )
    selected = ranked[:limit]
    return [
        {
            "id": item["id"],
            "prompt": item["prompt"],
            "placeholder": item["placeholder"],
            "help_text": item["help_text"],
        }
        for item in selected
    ]


def _match_profiles(answer_text: str, question: dict[str, Any]) -> tuple[dict[str, float], float, list[dict[str, Any]]]:
    normalized = _normalize_answer(answer_text)
    if not normalized or normalized in _GENERIC_ANSWERS:
        return {}, 0.0, []

    matches: list[dict[str, Any]] = []
    for profile in question["profiles"]:
        phrase_hits = [phrase for phrase in profile["phrases"] if phrase in normalized]
        if not phrase_hits:
            continue
        weight = 1.0 + (0.22 * (len(phrase_hits) - 1))
        matches.append(
            {
                "id": profile["id"],
                "weight": weight,
                "phrase_hits": phrase_hits[:5],
                "signals": profile["signals"],
                "confidence": float(profile["confidence"]),
            }
        )

    if not matches:
        return {}, 0.0, []

    total_weight = sum(item["weight"] for item in matches) or 1.0
    blended: dict[str, float] = {}
    for match in matches:
        for feature_name, value in match["signals"].items():
            blended[feature_name] = blended.get(feature_name, 0.0) + (float(value) * match["weight"])
    blended = {feature_name: value / total_weight for feature_name, value in blended.items()}
    confidence = min(
        0.92,
        max(item["confidence"] for item in matches) + (0.04 * max(0, len(matches) - 1)),
    )
    return blended, confidence, matches


def build_clarification_signal_map(clarification_answers: dict[str, str] | None) -> dict[str, dict[str, Any]]:
    if not clarification_answers:
        return {}

    aggregates: dict[str, dict[str, Any]] = {}
    for question in _QUESTION_BANK:
        answer_text = str(clarification_answers.get(question["id"]) or "").strip()
        if not answer_text:
            continue
        signals, confidence, matches = _match_profiles(answer_text, question)
        if not signals:
            continue
        for feature_name, value in signals.items():
            entry = aggregates.setdefault(
                feature_name,
                {
                    "weighted_value": 0.0,
                    "weight_total": 0.0,
                    "confidence": 0.0,
                    "question_ids": [],
                    "answer_texts": [],
                    "matched_profiles": [],
                },
            )
            entry["weighted_value"] += float(value) * confidence
            entry["weight_total"] += confidence
            entry["confidence"] = max(entry["confidence"], confidence)
            if question["id"] not in entry["question_ids"]:
                entry["question_ids"].append(question["id"])
            entry["answer_texts"].append(answer_text)
            entry["matched_profiles"].extend(
                {
                    "question_id": question["id"],
                    "profile_id": match["id"],
                    "phrase_hits": match["phrase_hits"],
                }
                for match in matches
            )

    signal_map: dict[str, dict[str, Any]] = {}
    for feature_name, entry in aggregates.items():
        weight_total = float(entry["weight_total"]) or 1.0
        signal_map[feature_name] = {
            "value": float(entry["weighted_value"]) / weight_total,
            "confidence": float(entry["confidence"]),
            "question_ids": entry["question_ids"],
            "answer_texts": entry["answer_texts"][:4],
            "matched_profiles": entry["matched_profiles"][:6],
        }
    return signal_map
