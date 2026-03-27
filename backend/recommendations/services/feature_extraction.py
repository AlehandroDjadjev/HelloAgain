from __future__ import annotations

import logging
import os
import re
from functools import lru_cache

import torch

from recommendations.gat.feature_schema import get_default_feature_vector, get_feature_names
from recommendations.services.intake_clarification import build_clarification_signal_map

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-zA-Zа-яА-Я0-9']+")
_NEGATION_WORDS = {
    "not",
    "no",
    "never",
    "dont",
    "don't",
    "cannot",
    "can't",
    "avoid",
    "avoids",
    "dislike",
    "dislikes",
    "hate",
    "hates",
    "не",
    "никога",
    "без",
    "никак",
    "никакъв",
    "никаква",
    "никакви",
}

_POSITIVE_PREFERENCE_VERBS = {
    "like",
    "likes",
    "love",
    "loves",
    "prefer",
    "prefers",
    "enjoy",
    "enjoys",
    "харесвам",
    "обичам",
    "предпочитам",
    "искам",
}

_NEGATIVE_PREFERENCE_VERBS = {
    "dislike",
    "dislikes",
    "hate",
    "hates",
    "avoid",
    "avoids",
    "мразя",
    "ненавиждам",
    "избягвам",
    "нехаресвам",
    "необичам",
    "неискам",
}

_OBJECT_STOPWORDS = {
    "и",
    "или",
    "за",
    "с",
    "в",
    "на",
    "по",
    "от",
    "to",
    "and",
    "or",
    "with",
    "for",
    "a",
    "an",
    "the",
    "аз",
    "ти",
    "той",
    "тя",
    "ние",
    "вие",
    "те",
}


def _normalize_token(token: str) -> str:
    normalized = token.lower().strip()
    if normalized.endswith("'s"):
        normalized = normalized[:-2]
    if len(normalized) > 4 and normalized.endswith("ies"):
        normalized = normalized[:-3] + "y"
    elif len(normalized) > 3 and normalized.endswith("s") and not normalized.endswith("ss"):
        normalized = normalized[:-1]
    return normalized

_FEATURE_RUBRICS: dict[str, dict[str, tuple[str, ...]]] = {
    "extroversion": {
        "positive": ("outgoing", "social", "friendly", "energetic", "people", "meeting"),
        "negative": ("quiet", "reserved", "shy", "private", "solitude", "alone"),
    },
    "openness": {
        "positive": ("curious", "learning", "new", "adventure", "creative", "open-minded"),
        "negative": ("routine", "predictable", "traditional", "fixed"),
    },
    "agreeableness": {
        "positive": ("kind", "gentle", "cooperative", "supportive", "easygoing"),
        "negative": ("stubborn", "argumentative", "combative"),
    },
    "emotional_warmth": {
        "positive": ("warm", "caring", "affectionate", "loving", "tender"),
        "negative": ("cold", "distant", "detached"),
    },
    "humor": {
        "positive": ("funny", "laugh", "jokes", "playful", "humor"),
        "negative": ("serious", "stern"),
    },
    "positivity": {
        "positive": ("positive", "optimistic", "hopeful", "grateful", "joyful"),
        "negative": ("pessimistic", "negative", "gloomy"),
    },
    "patience": {
        "positive": ("patient", "calm", "steady", "slow-paced"),
        "negative": ("impatient", "restless", "rushed"),
    },
    "empathy": {
        "positive": ("empathetic", "understanding", "listener", "compassionate"),
        "negative": ("self-centered", "insensitive"),
    },
    "interest_music": {
        "positive": ("music", "singing", "choir", "concert", "radio"),
        "negative": (),
    },
    "interest_nature": {
        "positive": ("nature", "garden", "gardening", "park", "flowers", "walking"),
        "negative": ("hate park", "hate parks", "dislike park", "dislike parks", "avoid park", "avoid parks"),
    },
    "interest_family": {
        "positive": ("family", "grandchildren", "children", "relatives", "home"),
        "negative": (),
    },
    "interest_cooking": {
        "positive": ("cooking", "baking", "recipes", "kitchen", "food"),
        "negative": (),
    },
    "interest_religion": {
        "positive": ("church", "faith", "religion", "prayer", "spiritual"),
        "negative": (),
    },
    "interest_history": {
        "positive": ("history", "historical", "past", "stories", "museum"),
        "negative": (),
    },
    "interest_travel": {
        "positive": ("travel", "trip", "journey", "explore", "visiting"),
        "negative": (),
    },
    "interest_arts": {
        "positive": ("art", "painting", "crafts", "theatre", "poetry"),
        "negative": (),
    },
    "verbosity": {
        "positive": ("talkative", "chatty", "conversation", "stories", "speak"),
        "negative": ("quiet", "few words", "brief"),
    },
    "formality": {
        "positive": ("formal", "proper", "polite", "respectful"),
        "negative": ("casual", "relaxed", "informal"),
    },
    "story_telling": {
        "positive": ("stories", "storytelling", "memories", "reminisce", "share experiences"),
        "negative": (),
    },
    "directness": {
        "positive": ("direct", "straightforward", "honest", "clear"),
        "negative": ("subtle", "indirect"),
    },
    "prefers_small_groups": {
        "positive": ("small group", "one-on-one", "quiet company", "intimate"),
        "negative": ("big crowd", "large group", "party"),
    },
    "activity_level": {
        "positive": ("active", "walking", "exercise", "busy", "moving"),
        "negative": ("resting", "slow", "sedentary"),
    },
    "nostalgia_index": {
        "positive": ("nostalgic", "memories", "old songs", "past", "remember"),
        "negative": (),
    },
    "spiritual_alignment": {
        "positive": ("spiritual", "faith", "prayer", "purpose", "meaning"),
        "negative": (),
    },
    "reliability": {
        "positive": ("reliable", "dependable", "consistent", "steady", "keeps promises"),
        "negative": ("unreliable", "forgetful", "inconsistent"),
    },
    "independence": {
        "positive": ("independent", "self-sufficient", "on my own", "personal space", "autonomy"),
        "negative": ("clingy", "dependent", "needs constant company"),
    },
    "adaptability": {
        "positive": ("adaptable", "flexible", "adjusts", "open to change"),
        "negative": ("rigid", "set in ways", "dislikes change"),
    },
    "emotional_stability": {
        "positive": ("steady", "stable", "calm under pressure", "even-tempered"),
        "negative": ("volatile", "moody", "easily upset"),
    },
    "assertiveness": {
        "positive": ("assertive", "takes charge", "speaks up", "confident"),
        "negative": ("hesitant", "passive", "withdrawn"),
    },
    "playfulness": {
        "positive": ("playful", "teasing", "lighthearted", "silly"),
        "negative": ("stern", "rigid", "severe"),
    },
    "curiosity": {
        "positive": ("curious", "asks questions", "interested in learning", "wondering"),
        "negative": ("disinterested", "uninterested", "closed off"),
    },
    "routine_orientation": {
        "positive": ("routine", "predictable", "same time", "habit"),
        "negative": ("spontaneous", "unplanned", "go with the flow"),
    },
    "interest_books": {
        "positive": ("books", "reading", "novels", "library", "magazine"),
        "negative": (),
    },
    "interest_sports": {
        "positive": (
            "sports",
            "football",
            "basketball",
            "tennis",
            "volleyball",
            "match",
            "training",
            "exercise",
        ),
        "negative": (
            "hate sports",
            "dislike sports",
            "avoid sports",
            "hate volleyball",
            "dislike volleyball",
            "avoid volleyball",
        ),
    },
    "interest_games": {
        "positive": ("games", "cards", "chess", "board games", "puzzles"),
        "negative": (),
    },
    "interest_crafts": {
        "positive": ("crafts", "knitting", "sewing", "crochet", "handmade"),
        "negative": (),
    },
    "interest_movies": {
        "positive": ("movies", "cinema", "films", "documentaries", "watching shows"),
        "negative": (),
    },
    "interest_pets": {
        "positive": ("pets", "dog", "cat", "animals", "pet"),
        "negative": (),
    },
    "interest_volunteering": {
        "positive": ("volunteer", "helping others", "community service", "charity"),
        "negative": (),
    },
    "interest_technology": {
        "positive": ("phone", "computer", "technology", "tablet", "internet"),
        "negative": (),
    },
    "listening_style": {
        "positive": ("good listener", "listens", "patient listener", "hears people out"),
        "negative": ("interrupts", "talks over people"),
    },
    "question_asking": {
        "positive": ("asks questions", "curious about people", "wants to know more"),
        "negative": (),
    },
    "conversation_depth": {
        "positive": ("deep conversations", "meaningful conversation", "thoughtful topics", "serious topics"),
        "negative": ("surface-level", "small talk only"),
    },
    "conflict_tact": {
        "positive": ("diplomatic", "tactful", "gentle in disagreement", "careful with conflict"),
        "negative": ("combative", "blunt in conflict", "argumentative"),
    },
    "responsiveness": {
        "positive": ("responsive", "checks in", "answers quickly", "follows up"),
        "negative": ("hard to reach", "doesn't reply", "detached"),
    },
    "reassurance_expression": {
        "positive": ("reassuring", "comforting", "encouraging", "puts people at ease"),
        "negative": (),
    },
    "pace_of_speech": {
        "positive": ("fast talker", "quick speech", "rapid conversation"),
        "negative": ("slow speaker", "measured speech", "speaks slowly"),
    },
    "prefers_structure": {
        "positive": ("structured", "organized", "clear plan", "schedule"),
        "negative": ("spontaneous", "unstructured"),
    },
    "community_involvement": {
        "positive": ("community", "neighbors", "club", "church group", "local group"),
        "negative": (),
    },
    "hosting_preference": {
        "positive": ("hosts", "invites people over", "welcomes visitors"),
        "negative": (),
    },
    "receiving_support_comfort": {
        "positive": ("comfortable asking for help", "accepts help", "welcomes support"),
        "negative": ("doesn't want help", "refuses help", "insists on doing everything alone"),
    },
    "needs_personal_space": {
        "positive": ("personal space", "space", "time alone", "independent routine"),
        "negative": ("always around people", "constant company"),
    },
    "schedule_flexibility": {
        "positive": ("flexible", "easygoing with plans", "open schedule"),
        "negative": ("strict schedule", "fixed routine"),
    },
    "tolerance_for_noise": {
        "positive": ("busy places", "noise doesn't bother", "lively atmosphere"),
        "negative": ("sensitive to noise", "quiet needed", "hates loud places"),
    },
    "mobility_confidence": {
        "positive": ("gets around easily", "walks confidently", "mobile"),
        "negative": ("unsteady", "limited mobility", "needs assistance walking"),
    },
    "self_sufficiency": {
        "positive": ("self-sufficient", "handles things alone", "manages independently"),
        "negative": ("needs a lot of help", "depends on others"),
    },
    "practical_help_orientation": {
        "positive": ("practical help", "runs errands", "helps with tasks", "fixes things"),
        "negative": (),
    },
    "caretaking_drive": {
        "positive": ("takes care of others", "caretaker", "looks after people"),
        "negative": (),
    },
    "health_caution": {
        "positive": ("careful with health", "cautious", "plays it safe"),
        "negative": ("reckless", "ignores limitations"),
    },
    "adventure_comfort": {
        "positive": ("adventurous", "tries new things", "up for outings"),
        "negative": ("doesn't like risk", "avoids new experiences"),
    },
    "change_tolerance": {
        "positive": ("handles change well", "adjusts to change", "flexible with change"),
        "negative": ("struggles with change", "upset by changes"),
    },
    "financial_caution": {
        "positive": ("careful with money", "budget", "frugal", "saves"),
        "negative": ("spends freely", "impulsive with money"),
    },
    "local_attachment": {
        "positive": ("loves the neighborhood", "attached to home area", "local places"),
        "negative": ("ready to move anywhere", "not attached to place"),
    },
    "pet_attachment": {
        "positive": ("adores animals", "loves pets", "pet companion"),
        "negative": (),
    },
}


def _tokenize(text: str) -> list[str]:
    return [_normalize_token(token) for token in _TOKEN_RE.findall(text)]


def _phrase_is_negated(tokens: list[str], phrase: str) -> bool:
    phrase_tokens = [_normalize_token(token) for token in _TOKEN_RE.findall(phrase)]
    if not phrase_tokens:
        return False
    size = len(phrase_tokens)
    for index in range(0, max(0, len(tokens) - size + 1)):
        if tokens[index : index + size] != phrase_tokens:
            continue
        window_start = max(0, index - 3)
        context = tokens[window_start:index]
        if any(token in _NEGATION_WORDS for token in context):
            return True
    return False


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


@lru_cache(maxsize=1)
def _load_embedding_model():
    enabled = os.getenv("ENABLE_SEMANTIC_EMBEDDINGS", "0").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None

    try:
        from sentence_transformers import SentenceTransformer
    except Exception:
        return None
    for model_name in (
        "paraphrase-multilingual-MiniLM-L12-v2",
        "all-MiniLM-L6-v2",
    ):
        try:
            return SentenceTransformer(model_name)
        except Exception:
            continue
    return None


def extract_preference_intents(description: str) -> list[dict[str, object]]:
    tokens = _tokenize(description)
    if not tokens:
        return []

    intents: list[dict[str, object]] = []
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        polarity = 0
        action = ""
        object_start = idx + 1
        consumed_next_token = False

        if token in _POSITIVE_PREFERENCE_VERBS:
            if idx > 0 and tokens[idx - 1] in _NEGATION_WORDS:
                idx += 1
                continue
            polarity = 1
            action = token
        elif token in _NEGATIVE_PREFERENCE_VERBS:
            if idx > 0 and tokens[idx - 1] in _NEGATION_WORDS:
                idx += 1
                continue
            polarity = -1
            action = token
        elif token in _NEGATION_WORDS and idx + 1 < len(tokens):
            next_token = tokens[idx + 1]
            if next_token in _POSITIVE_PREFERENCE_VERBS:
                polarity = -1
                action = f"{token} {next_token}"
                object_start = idx + 2
                consumed_next_token = True
            elif next_token in _NEGATIVE_PREFERENCE_VERBS:
                polarity = 1
                action = f"{token} {next_token}"
                object_start = idx + 2
                consumed_next_token = True

        if polarity == 0:
            idx += 1
            continue

        object_token = ""
        for look_ahead in range(object_start, min(len(tokens), object_start + 4)):
            candidate = tokens[look_ahead]
            if (
                candidate in _NEGATION_WORDS
                or candidate in _POSITIVE_PREFERENCE_VERBS
                or candidate in _NEGATIVE_PREFERENCE_VERBS
                or candidate in _OBJECT_STOPWORDS
                or len(candidate) < 2
            ):
                continue
            object_token = candidate
            break

        if not object_token:
            idx += 2 if consumed_next_token else 1
            continue

        intents.append(
            {
                "subject": "I",
                "action": action,
                "object": object_token,
                "polarity": polarity,
            }
        )
        idx += 2 if consumed_next_token else 1

    # Keep first intent per (object, polarity) to avoid token-repeat dominance.
    dedup: dict[tuple[str, int], dict[str, object]] = {}
    for intent in intents:
        key = (str(intent["object"]), int(intent["polarity"]))
        if key not in dedup:
            dedup[key] = intent
    return list(dedup.values())


def summarize_preference_intents(intents: list[dict[str, object]]) -> dict[str, float]:
    summary: dict[str, float] = {}
    for intent in intents:
        obj = str(intent.get("object", "")).strip()
        if not obj:
            continue
        polarity = float(intent.get("polarity", 0.0))
        summary[obj] = max(-1.0, min(1.0, polarity))
    return summary


def embedding_pair_similarity(left_text: str, right_text: str) -> dict[str, object]:
    model = _load_embedding_model()
    if model is None:
        return {
            "backend": "rules_only",
            "cosine": None,
            "left_preview": [],
            "right_preview": [],
        }

    embeddings = model.encode([left_text or "", right_text or ""], convert_to_tensor=True)
    left_embedding, right_embedding = embeddings
    cosine = float(torch.nn.functional.cosine_similarity(left_embedding, right_embedding, dim=0).item())
    left_preview = [round(float(item), 4) for item in left_embedding[:6].tolist()]
    right_preview = [round(float(item), 4) for item in right_embedding[:6].tolist()]
    left_norm = float(torch.linalg.norm(left_embedding).item())
    right_norm = float(torch.linalg.norm(right_embedding).item())
    return {
        "backend": "sentence_transformers",
        "cosine": round(cosine, 4),
        "left_preview": left_preview,
        "right_preview": right_preview,
        "left_norm": round(left_norm, 4),
        "right_norm": round(right_norm, 4),
    }


@lru_cache(maxsize=1)
def _feature_prototypes() -> dict[str, tuple[str, str]]:
    prototypes: dict[str, tuple[str, str]] = {}
    for feature_name, rubric in _FEATURE_RUBRICS.items():
        positive = rubric.get("positive") or ()
        negative = rubric.get("negative") or ()
        positive_text = f"A person who is strongly {feature_name.replace('_', ' ')} and enjoys {' '.join(positive[:4])}."
        negative_text = f"A person who is low in {feature_name.replace('_', ' ')} and leans toward {' '.join(negative[:4]) or 'neutral behavior'}."
        prototypes[feature_name] = (positive_text, negative_text)
    return prototypes


def _embedding_score(description: str, feature_name: str) -> tuple[float, dict]:
    model = _load_embedding_model()
    if model is None:
        return 0.5, {"backend": "rules_only", "margin": 0.0}

    positive_text, negative_text = _feature_prototypes()[feature_name]
    embeddings = model.encode([description, positive_text, negative_text], convert_to_tensor=True)
    description_embedding, positive_embedding, negative_embedding = embeddings
    pos_score = float(torch.nn.functional.cosine_similarity(description_embedding, positive_embedding, dim=0).item())
    neg_score = float(torch.nn.functional.cosine_similarity(description_embedding, negative_embedding, dim=0).item())
    margin = pos_score - neg_score
    normalized = max(0.0, min(1.0, 0.5 + (margin * 0.5)))
    return normalized, {
        "backend": "sentence_transformers",
        "positive_similarity": round(pos_score, 4),
        "negative_similarity": round(neg_score, 4),
        "margin": round(margin, 4),
    }


def _rubric_score(tokens: list[str], description: str, feature_name: str) -> tuple[float, dict]:
    rubric = _FEATURE_RUBRICS.get(feature_name, {"positive": (), "negative": ()})
    positives = rubric.get("positive") or ()
    negatives = rubric.get("negative") or ()
    positive_hits: list[str] = []
    negative_hits: list[str] = []
    negated_positive_hits: list[str] = []
    negated_negative_hits: list[str] = []

    for phrase in positives:
        phrase_lc = phrase.lower()
        phrase_tokens = [_normalize_token(token) for token in _TOKEN_RE.findall(phrase_lc)]
        phrase_present = (phrase_lc in description) or (
            len(phrase_tokens) == 1 and phrase_tokens[0] in tokens
        )
        if not phrase_present:
            continue
        if _phrase_is_negated(tokens, phrase_lc):
            negated_positive_hits.append(phrase)
            negative_hits.append(phrase)
        else:
            positive_hits.append(phrase)

    for phrase in negatives:
        phrase_lc = phrase.lower()
        phrase_tokens = [_normalize_token(token) for token in _TOKEN_RE.findall(phrase_lc)]
        phrase_present = (phrase_lc in description) or (
            len(phrase_tokens) == 1 and phrase_tokens[0] in tokens
        )
        if not phrase_present:
            continue
        if _phrase_is_negated(tokens, phrase_lc):
            negated_negative_hits.append(phrase)
            positive_hits.append(phrase)
        else:
            negative_hits.append(phrase)
    score = 0.5
    if positive_hits:
        score += min(0.42, 0.18 * len(positive_hits))
    if negative_hits:
        score -= min(0.42, 0.18 * len(negative_hits))
    return _clamp(score), {
        "positive_hits": positive_hits[:5],
        "negative_hits": negative_hits[:5],
        "negated_positive_hits": negated_positive_hits[:5],
        "negated_negative_hits": negated_negative_hits[:5],
        "signal_strength": round(
            abs(len(positive_hits) - len(negative_hits)) / max(1, len(positives) + len(negatives)),
            4,
        ),
        "direct_hits": len(positive_hits) + len(negative_hits),
    }


def _neutrality_clamp(raw_score: float, *, confidence: float, direct_hits: int) -> float:
    if direct_hits >= 1:
        return _clamp(raw_score)
    # Reduced dampening so features spread further from neutral
    dampening = 0.20 + (0.60 * confidence)
    return _clamp(0.5 + ((raw_score - 0.5) * dampening))


def extract_feature_profile(
    description: str,
    *,
    manual_overrides: dict[str, float] | None = None,
    clarification_answers: dict[str, str] | None = None,
) -> dict[str, dict]:
    defaults = get_default_feature_vector()
    feature_names = get_feature_names()
    normalized_description = " ".join(_tokenize(description))
    tokens = _tokenize(description)
    semantic_intents = extract_preference_intents(description)
    semantic_intent_summary = summarize_preference_intents(semantic_intents)
    overrides = manual_overrides or {}
    clarification_signals = build_clarification_signal_map(clarification_answers)
    result: dict[str, dict] = {}

    logger.info(
        "feature_extraction.semantic_intents intents=%s summary=%s",
        semantic_intents,
        semantic_intent_summary,
    )

    for feature_name in feature_names:
        default_value = float(defaults.get(feature_name, 0.5))
        rubric_score, rubric_evidence = _rubric_score(tokens, normalized_description, feature_name)
        semantic_score, semantic_evidence = _embedding_score(description or feature_name.replace("_", " "), feature_name)
        semantic_margin = abs(float(semantic_evidence.get("margin", 0.0)))
        direct_hits = int(rubric_evidence.get("direct_hits", 0))

        if direct_hits:
            raw_value = (0.72 * rubric_score) + (0.18 * default_value) + (0.10 * semantic_score)
            source = "rules_primary"
            confidence = min(
                0.97,
                0.48
                + (0.15 * direct_hits)
                + (0.12 * rubric_evidence["signal_strength"])
                + (0.06 * min(1.0, semantic_margin * 2.0)),
            )
        elif semantic_evidence.get("backend") == "sentence_transformers" and semantic_margin >= 0.18:
            raw_value = default_value + ((semantic_score - 0.5) * 0.28)
            source = "semantic_hint"
            confidence = min(0.58, 0.30 + (0.35 * semantic_margin))
        else:
            raw_value = 0.5 + ((semantic_score - 0.5) * 0.14)
            source = "neutral"
            confidence = min(0.42, 0.22 + (0.22 * semantic_margin))

        raw_value = _neutrality_clamp(raw_value, confidence=confidence, direct_hits=direct_hits)

        clarification_evidence = None
        if feature_name in clarification_signals:
            clarification_evidence = clarification_signals[feature_name]
            raw_value = (0.25 * raw_value) + (0.75 * float(clarification_evidence["value"]))
            confidence = max(confidence, float(clarification_evidence["confidence"]))
            source = "clarified_hybrid"

        if feature_name in overrides:
            raw_value = float(_clamp(overrides[feature_name]))
            source = "manual_override"
            confidence = 1.0

        raw_value = _clamp(raw_value)
        confidence = _clamp(confidence)
        # Amplified effective value: use confidence^0.7 so that moderate
        # confidence (0.4–0.7) lets more signal through instead of
        # compressing everything toward 0.5.
        amplified_conf = min(1.0, confidence ** 0.7)
        effective_value = _clamp(0.5 + ((raw_value - 0.5) * amplified_conf))

        result[feature_name] = {
            "value": round(effective_value, 4),
            "raw_value": round(raw_value, 4),
            "effective_value": round(effective_value, 4),
            "confidence": round(confidence, 4),
            "source": source,
            "evidence": {
                "rubric": rubric_evidence,
                "semantic": semantic_evidence,
                "default": default_value,
                "clarification": clarification_evidence,
            },
        }
    return result


def extraction_to_vectors(
    extraction: dict[str, dict],
) -> tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, dict], dict[str, str]]:
    raw_values = {feature_name: payload.get("raw_value", payload["value"]) for feature_name, payload in extraction.items()}
    effective_values = {
        feature_name: payload.get("effective_value", payload["value"])
        for feature_name, payload in extraction.items()
    }
    confidence = {feature_name: payload["confidence"] for feature_name, payload in extraction.items()}
    evidence = {feature_name: payload["evidence"] for feature_name, payload in extraction.items()}
    sources = {feature_name: payload["source"] for feature_name, payload in extraction.items()}
    return raw_values, effective_values, confidence, evidence, sources
