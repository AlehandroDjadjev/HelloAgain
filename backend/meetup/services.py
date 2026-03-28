import logging
import math
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from recommendations.gat.feature_schema import get_default_feature_vector
from recommendations.services.compatibility_engine import compare_people


_WEATHER_BG_MAP = {
    'Clear': 'Ясно',
    'Clouds': 'Облачно',
    'Rain': 'Дъжд',
    'Drizzle': 'Ръмеж',
    'Thunderstorm': 'Гръмотевици',
    'Snow': 'Сняг',
    'Mist': 'Мъгла',
    'Fog': 'Мъгла',
    'Haze': 'Мараня',
    'Smoke': 'Дим',
    'Dust': 'Прах',
    'Sand': 'Пясък',
    'Ash': 'Пепел',
    'Squall': 'Шквал',
    'Tornado': 'Торнадо',
}

_PLACE_TYPE_BG_MAP = {
    'park': 'парк',
    'cafe': 'кафене',
    'restaurant': 'ресторант',
    'museum': 'музей',
    'library': 'библиотека',
    'movie_theater': 'кино',
    'shopping_mall': 'търговски център',
    'gym': 'спортна зала',
    'book_store': 'книжарница',
}

logger = logging.getLogger(__name__)
_BULGARIA_TZ = ZoneInfo('Europe/Sofia')
_PLACE_COMPATIBILITY_WEIGHT = 0.52
_DISTANCE_WEIGHT = 0.18
_REVIEWS_WEIGHT = 0.10


def _to_bg_weather(label):
    if not label:
        return ''
    return _WEATHER_BG_MAP.get(label, label)


def _to_bg_place_type(label):
    if not label:
        return ''
    return _PLACE_TYPE_BG_MAP.get(label, label)

_BG_WEEKDAYS = {
    0: 'понеделник',
    1: 'вторник',
    2: 'сряда',
    3: 'четвъртък',
    4: 'петък',
    5: 'събота',
    6: 'неделя',
}

def _format_meetup_when_bg(dt):
    weekday = _BG_WEEKDAYS.get(dt.weekday(), '')
    date_part = dt.strftime('%d.%m.%Y')
    time_part = dt.strftime('%H:%M')
    return {
        'recommended_day_bg': weekday,
        'recommended_date_bg': date_part,
        'recommended_time_bg': time_part,
        'recommended_when_bg': f'{weekday}, {date_part} в {time_part}',
    }

_DEFAULT_PLACE_WEIGHTS = {
    'park': 1.0,
    'cafe': 1.05,
    'library': 0.68,
    'museum': 0.62,
    'restaurant': 0.42,
    'shopping_mall': 0.45,
}

_PLACE_TO_DOMAIN = {
    'park': 'outdoor',
    'playground': 'outdoor',
    'tourist_attraction': 'outdoor',
    'cafe': 'social',
    'coffee_shop': 'social',
    'bakery': 'social',
    'library': 'intellectual',
    'book_store': 'intellectual',
    'museum': 'intellectual',
    'art_gallery': 'intellectual',
    'restaurant': 'formal',
    'shopping_mall': 'casual',
}

_NEGATION_TOKENS = {
    'no',
    'not',
    'never',
    'hate',
    'dislike',
    'avoid',
    'dont',
    'do',
    'without',
    'не',
    'никога',
    'без',
    'никак',
}

_SEMANTIC_RULES = {
    'sports': ['sport', 'sports', 'volleyball', 'football', 'basketball', 'run', 'running', 'gym', 'спорт', 'спортове', 'волейбол', 'футбол', 'баскетбол', 'тичане', 'зала'],
    'books': ['book', 'books', 'reading', 'read', 'library', 'книга', 'книги', 'четене', 'чета', 'библиотека'],
    'history': ['history', 'historical', 'museum', 'museums', 'история', 'исторически', 'музей', 'музеи'],
    'music': ['music', 'concert', 'band', 'song', 'songs', 'музика', 'концерт', 'песен', 'песни'],
    'nature': ['nature', 'park', 'outdoor', 'walk', 'walking', 'hike', 'природа', 'парк', 'парка', 'паркове', 'разходка', 'разходки', 'навън'],
    'deep_talk': ['deep', 'deeply', 'meaningful', 'discussion', 'intellectual', 'thoughtful'],
    'casual_hangout': ['casual', 'chill', 'hangout', 'relax', 'relaxed', 'coffee', 'спокойно', 'лежерно', 'кафе'],
    'indoor': ['indoor', 'inside', 'library', 'museum', 'cafe', 'вътре', 'закрито'],
    'outdoor': ['outdoor', 'outside', 'park', 'hike', 'навън', 'открито', 'парк', 'парка', 'паркове'],
    'restaurant': ['restaurant', 'fine dining', 'formal dinner', 'ресторант', 'вечеря'],
    'park': ['park', 'parks', 'парк', 'парка', 'паркове'],
    'cafe': ['cafe', 'coffee', 'coffee shop', 'кафе', 'кафене', 'сладкарница'],
    'weather_sunny': ['sunny', 'clear sky', 'sun', 'warm weather'],
    'weather_rain_avoid': ['avoid rain', 'no rain', 'dont like rain', 'dislike rain', 'hate rain'],
    'weather_wind_avoid': ['avoid wind', 'no wind', 'dont like wind', 'dislike wind'],
    'weather_cool': ['cool weather', 'cool breeze', 'fresh weather'],
    'weather_warm': ['warm weather', 'hot weather', 'sunny weather'],
}

_WEATHER_WEIGHT = 0.20
_GRAPH_PLACE_BONUS_WEIGHT = 0.10

_PLACE_FEATURE_PRIORS = {
    'park': {
        'interest_nature': 0.92,
        'activity_level': 0.74,
        'adventure_comfort': 0.66,
        'mobility_confidence': 0.62,
        'tolerance_for_noise': 0.40,
        'prefers_small_groups': 0.58,
    },
    'playground': {
        'interest_nature': 0.82,
        'activity_level': 0.70,
        'adventure_comfort': 0.62,
        'mobility_confidence': 0.60,
    },
    'tourist_attraction': {
        'interest_nature': 0.68,
        'interest_history': 0.60,
        'openness': 0.62,
        'activity_level': 0.58,
    },
    'cafe': {
        'emotional_warmth': 0.68,
        'conversation_depth': 0.68,
        'prefers_small_groups': 0.76,
        'formality': 0.42,
        'activity_level': 0.42,
        'tolerance_for_noise': 0.60,
    },
    'coffee_shop': {
        'emotional_warmth': 0.66,
        'conversation_depth': 0.66,
        'prefers_small_groups': 0.74,
        'formality': 0.40,
        'activity_level': 0.42,
        'tolerance_for_noise': 0.62,
    },
    'bakery': {
        'emotional_warmth': 0.62,
        'conversation_depth': 0.58,
        'prefers_small_groups': 0.68,
        'formality': 0.38,
    },
    'library': {
        'interest_books': 0.94,
        'conversation_depth': 0.80,
        'prefers_small_groups': 0.84,
        'tolerance_for_noise': 0.22,
        'activity_level': 0.30,
    },
    'book_store': {
        'interest_books': 0.88,
        'conversation_depth': 0.72,
        'prefers_small_groups': 0.76,
        'tolerance_for_noise': 0.30,
    },
    'museum': {
        'interest_history': 0.86,
        'interest_arts': 0.78,
        'conversation_depth': 0.74,
        'prefers_small_groups': 0.68,
        'activity_level': 0.44,
    },
    'art_gallery': {
        'interest_arts': 0.88,
        'openness': 0.70,
        'conversation_depth': 0.68,
        'prefers_small_groups': 0.62,
    },
    'restaurant': {
        'formality': 0.72,
        'interest_cooking': 0.62,
        'emotional_warmth': 0.62,
        'prefers_small_groups': 0.64,
        'financial_caution': 0.40,
    },
    'shopping_mall': {
        'activity_level': 0.60,
        'tolerance_for_noise': 0.80,
        'schedule_flexibility': 0.62,
        'openness': 0.58,
    },
}

_MEETUP_GRAPH_FEATURES = [
    'emotional_warmth',
    'openness',
    'interest_nature',
    'interest_arts',
    'interest_books',
    'interest_history',
    'interest_music',
    'interest_sports',
    'interest_cooking',
    'formality',
    'conversation_depth',
    'prefers_small_groups',
    'activity_level',
    'tolerance_for_noise',
    'mobility_confidence',
    'adventure_comfort',
    'schedule_flexibility',
    'financial_caution',
]


def _bounded(value):
    return max(0.0, min(1.0, float(value)))


def _tokenize_text(raw_text):
    return re.findall(r"[a-zа-я0-9']+", (raw_text or '').lower())


def _count_rule_hits(tokens, phrases):
    hits = 0
    for phrase in phrases:
        phrase_tokens = _tokenize_text(phrase)
        if not phrase_tokens:
            continue
        phrase_len = len(phrase_tokens)
        for idx in range(0, max(0, len(tokens) - phrase_len + 1)):
            window = tokens[idx : idx + phrase_len]
            if window != phrase_tokens:
                continue
            negated = False
            # Wider window catches patterns like "не харесвам ... в парка".
            start = max(0, idx - 5)
            for back in range(start, idx):
                if tokens[back] in _NEGATION_TOKENS:
                    negated = True
                    break
            hits += -1 if negated else 1
    return hits


def _semantic_profile_from_description(description):
    tokens = _tokenize_text(description)
    if not tokens:
        return {
            'features': {
                'sports': 0.5,
                'books': 0.5,
                'history': 0.5,
                'music': 0.5,
                'nature': 0.5,
            },
            'social_style': {'deep_talk': 0.5, 'casual_hangout': 0.5},
            'environment': {'indoor': 0.5, 'outdoor': 0.5},
            'activity_level': 0.5,
            'meeting_type': {'active': 0.33, 'chill': 0.33, 'intellectual': 0.34},
            'weather_preferences': {
                'sunny_preference': 0.5,
                'rain_tolerance': 0.5,
                'wind_tolerance': 0.5,
                'temp_target_c': 20.0,
            },
            'confidence': 0.2,
            'explicit_preferences': {},
        }

    raw_hits = {name: _count_rule_hits(tokens, phrases) for name, phrases in _SEMANTIC_RULES.items()}

    def _normalize_hit(hit):
        return _bounded(0.5 + (0.18 * hit))

    interests = {
        'sports': _normalize_hit(raw_hits['sports']),
        'books': _normalize_hit(raw_hits['books']),
        'history': _normalize_hit(raw_hits['history']),
        'music': _normalize_hit(raw_hits['music']),
        'nature': _normalize_hit(raw_hits['nature']),
    }
    social_style = {
        'deep_talk': _normalize_hit(raw_hits['deep_talk']),
        'casual_hangout': _normalize_hit(raw_hits['casual_hangout']),
    }
    environment = {
        'indoor': _normalize_hit(raw_hits['indoor']),
        'outdoor': _normalize_hit(raw_hits['outdoor']),
    }

    activity_level = _bounded((interests['sports'] * 0.62) + (interests['nature'] * 0.38))
    meeting_type = {
        'active': _bounded((interests['sports'] * 0.58) + (environment['outdoor'] * 0.42)),
        'chill': _bounded((social_style['casual_hangout'] * 0.55) + (interests['music'] * 0.20) + 0.20),
        'intellectual': _bounded((interests['books'] * 0.42) + (interests['history'] * 0.38) + (social_style['deep_talk'] * 0.20)),
    }

    explicit_preferences = {
        'park': _normalize_hit(raw_hits['park']),
        'cafe': _normalize_hit(raw_hits['cafe']),
        'restaurant': _normalize_hit(raw_hits['restaurant']),
    }

    sunny_preference = _normalize_hit(raw_hits['weather_sunny'])
    rain_avoid_signal = _normalize_hit(raw_hits['weather_rain_avoid'])
    wind_avoid_signal = _normalize_hit(raw_hits['weather_wind_avoid'])
    cool_signal = _normalize_hit(raw_hits['weather_cool'])
    warm_signal = _normalize_hit(raw_hits['weather_warm'])

    weather_preferences = {
        'sunny_preference': sunny_preference,
        'rain_tolerance': _bounded(1.0 - (rain_avoid_signal - 0.5)),
        'wind_tolerance': _bounded(1.0 - (wind_avoid_signal - 0.5)),
        'temp_target_c': max(8.0, min(30.0, 20.0 + ((warm_signal - cool_signal) * 10.0))),
    }
    confidence = _bounded(min(1.0, len(tokens) / 18.0))

    profile = {
        'features': interests,
        'social_style': social_style,
        'environment': environment,
        'activity_level': activity_level,
        'meeting_type': meeting_type,
        'weather_preferences': weather_preferences,
        'confidence': confidence,
        'explicit_preferences': explicit_preferences,
    }
    logger.info('meetup.semantic_profile=%s', profile)
    return profile


def _vector_from_semantic_profile(profile):
    vector = get_default_feature_vector()
    vector.update(
        {
            'emotional_warmth': _bounded(
                (profile['social_style']['casual_hangout'] * 0.45) + 0.30
            ),
            'openness': _bounded(
                (profile['features']['history'] * 0.25)
                + (profile['features']['music'] * 0.20)
                + (profile['features']['nature'] * 0.20)
                + 0.20
            ),
            'interest_nature': profile['features']['nature'],
            'interest_arts': _bounded(
                (profile['features']['history'] * 0.55)
                + (profile['features']['music'] * 0.45)
            ),
            'interest_books': profile['features']['books'],
            'interest_history': profile['features']['history'],
            'interest_sports': profile['features']['sports'],
            'interest_music': profile['features']['music'],
            'formality': _bounded(
                0.65 - (profile['social_style']['casual_hangout'] * 0.25)
            ),
            'conversation_depth': _bounded(
                (profile['social_style']['deep_talk'] * 0.70) + 0.15
            ),
            'prefers_small_groups': _bounded(
                (profile['social_style']['deep_talk'] * 0.66) + 0.20
            ),
            'activity_level': profile['activity_level'],
            'tolerance_for_noise': _bounded(
                (profile['environment']['outdoor'] * 0.35)
                + (profile['social_style']['casual_hangout'] * 0.25)
                + 0.20
            ),
            'mobility_confidence': _bounded(
                (profile['activity_level'] * 0.45)
                + (profile['environment']['outdoor'] * 0.25)
                + 0.20
            ),
            'adventure_comfort': _bounded(
                (profile['meeting_type']['active'] * 0.55)
                + (profile['environment']['outdoor'] * 0.20)
                + 0.20
            ),
            'schedule_flexibility': _bounded(0.35 + (profile['confidence'] * 0.30)),
            'financial_caution': 0.50,
        }
    )
    return vector


def _place_feature_vector(place_types, averaged_profile=None):
    vector = get_default_feature_vector()
    priors = []
    for place_type in place_types or []:
        prior = _PLACE_FEATURE_PRIORS.get(place_type)
        if prior:
            priors.append(prior)
    if not priors:
        return vector

    for feature_name in _MEETUP_GRAPH_FEATURES:
        prior_values = [
            float(prior[feature_name])
            for prior in priors
            if feature_name in prior
        ]
        if prior_values:
            vector[feature_name] = sum(prior_values) / len(prior_values)

    explicit = (averaged_profile or {}).get('explicit_preferences', {})
    if explicit:
        vector['interest_nature'] = _bounded(
            (0.75 * vector.get('interest_nature', 0.5))
            + (0.25 * float(explicit.get('park', 0.5)))
        )
        vector['conversation_depth'] = _bounded(
            (0.80 * vector.get('conversation_depth', 0.5))
            + (0.20 * float(explicit.get('cafe', 0.5)))
        )
    return vector


def _graph_place_affinity_score(place_types, participant_vectors, averaged_profile):
    vectors = [
        item for item in (participant_vectors or []) if isinstance(item, dict) and item
    ]
    if not vectors:
        return 0.5

    place_vector = _place_feature_vector(place_types, averaged_profile)
    place_scores = []
    for vector in vectors:
        comparison = compare_people(
            vector,
            place_vector,
            graph_score=0.5,
            embedding_score=0.5,
            features=_MEETUP_GRAPH_FEATURES,
        )
        place_scores.append(float(comparison.get('compatibility_score', 0.5)))

    if not place_scores:
        return 0.5

    pairwise_scores = []
    if len(vectors) >= 2:
        for index in range(len(vectors)):
            for offset in range(index + 1, len(vectors)):
                comparison = compare_people(
                    vectors[index],
                    vectors[offset],
                    graph_score=0.5,
                    embedding_score=0.5,
                    features=_MEETUP_GRAPH_FEATURES,
                )
                pairwise_scores.append(
                    float(comparison.get('compatibility_score', 0.5))
                )

    avg_place_score = sum(place_scores) / len(place_scores)
    min_place_score = min(place_scores)
    pair_cohesion = (
        sum(pairwise_scores) / len(pairwise_scores) if pairwise_scores else avg_place_score
    )
    return _bounded(
        (0.55 * avg_place_score) + (0.25 * min_place_score) + (0.20 * pair_cohesion)
    )


def _pair_user_similarity(profiles):
    if len(profiles) < 2:
        return 0.55

    left = profiles[0]
    right = profiles[1]
    niche_features = ['sports', 'books', 'history', 'music']
    generic_features = ['nature']

    niche_similarity = 0.0
    niche_weight = 0.0
    contradictions = 0
    for key in niche_features:
        l = left['features'][key]
        r = right['features'][key]
        gap = abs(l - r)
        same_direction = (l - 0.5) * (r - 0.5) >= 0
        if not same_direction and abs(l - 0.5) > 0.16 and abs(r - 0.5) > 0.16:
            contradictions += 1
        feature_score = _bounded(1.0 - gap)
        niche_similarity += feature_score * 1.25
        niche_weight += 1.25

    generic_similarity = 0.0
    for key in generic_features:
        l = left['features'][key]
        r = right['features'][key]
        generic_similarity += _bounded(1.0 - abs(l - r)) * 0.5
        same_direction = (l - 0.5) * (r - 0.5) >= 0
        if not same_direction and abs(l - 0.5) > 0.20 and abs(r - 0.5) > 0.20:
            contradictions += 1

    for key in ['park', 'cafe', 'restaurant']:
        l = left.get('explicit_preferences', {}).get(key, 0.5)
        r = right.get('explicit_preferences', {}).get(key, 0.5)
        same_direction = (l - 0.5) * (r - 0.5) >= 0
        if not same_direction and abs(l - 0.5) > 0.16 and abs(r - 0.5) > 0.16:
            contradictions += 1
        if l >= 0.62 and r <= 0.38:
            contradictions += 2
        if r >= 0.62 and l <= 0.38:
            contradictions += 2

    social_score = _bounded(1.0 - abs(left['social_style']['deep_talk'] - right['social_style']['deep_talk']))
    activity_score = _bounded(1.0 - abs(left['activity_level'] - right['activity_level']))
    environment_score = _bounded(1.0 - abs(left['environment']['outdoor'] - right['environment']['outdoor']))

    base = (niche_similarity + generic_similarity + (social_score * 0.8) + (activity_score * 0.8) + (environment_score * 0.7)) / (
        niche_weight + 0.5 + 0.8 + 0.8 + 0.7
    )
    base -= 0.32 * contradictions
    base = _bounded(base)
    logger.info(
        'meetup.user_similarity base=%.3f contradictions=%s social=%.3f activity=%.3f environment=%.3f',
        base,
        contradictions,
        social_score,
        activity_score,
        environment_score,
    )
    return base


def _meeting_type_place_affinity(place_types, averaged_profile):
    meeting_type = averaged_profile.get('meeting_type', {})
    active = meeting_type.get('active', 0.33)
    chill = meeting_type.get('chill', 0.33)
    intellectual = meeting_type.get('intellectual', 0.34)

    domain_hits = {'outdoor': 0.0, 'social': 0.0, 'intellectual': 0.0, 'formal': 0.0, 'casual': 0.0}
    for place_type in place_types:
        domain = _PLACE_TO_DOMAIN.get(place_type)
        if domain:
            domain_hits[domain] = 1.0

    score = 0.0
    score += active * domain_hits['outdoor']
    score += chill * (0.8 * domain_hits['social'] + 0.2 * domain_hits['casual'])
    score += intellectual * domain_hits['intellectual']

    explicit = averaged_profile.get('explicit_preferences', {})
    explicit_conflicts = averaged_profile.get('explicit_conflicts', {})
    if domain_hits['outdoor'] > 0:
        score += explicit.get('park', 0.5) * 0.40
    if domain_hits['social'] > 0:
        score += explicit.get('cafe', 0.5) * 0.35
    if domain_hits['formal'] > 0:
        score += explicit.get('restaurant', 0.5) * 0.20
        if explicit.get('restaurant', 0.5) < 0.45:
            score -= 0.25

    # If both users strongly express park preference, avoid drifting to indoor alternatives.
    park_pref = explicit.get('park', 0.5)
    park_conflict = float(explicit_conflicts.get('park', 0.0))
    if park_pref >= 0.70 and domain_hits['outdoor'] > 0:
        score += 0.35
    elif park_pref >= 0.70 and domain_hits['outdoor'] == 0:
        score -= 0.40
    elif park_pref <= 0.40 and domain_hits['outdoor'] > 0:
        score -= 0.45
    elif park_pref <= 0.40 and domain_hits['social'] > 0:
        score += 0.20
    if park_conflict >= 0.30 and domain_hits['outdoor'] > 0:
        score -= 0.40
    if park_conflict >= 0.30 and domain_hits['social'] > 0:
        score += 0.15

    # Museum/library should not dominate unless the pair is genuinely intellectual-leaning.
    if 'museum' in place_types and intellectual < 0.58:
        score -= 0.25
    if 'library' in place_types and intellectual < 0.54:
        score -= 0.18
    return _bounded(score / 1.75)


def _time_window_weight(dt_obj):
    hour = dt_obj.hour
    minute = dt_obj.minute
    total_minutes = (hour * 60) + minute

    # Requested weighting windows:
    # 07:00-10:00 -> ~0.6-0.7
    if 420 <= total_minutes <= 600:
        return 0.65

    # 16:30-19:00 -> ~0.3-0.4
    if 990 <= total_minutes <= 1140:
        return 0.35

    return 0.50


def _average_semantic_profile(profiles):
    if not profiles:
        return _semantic_profile_from_description('')
    base = _semantic_profile_from_description('')
    for profile in profiles:
        for key in base['features']:
            base['features'][key] += profile['features'][key]
        for key in base['social_style']:
            base['social_style'][key] += profile['social_style'][key]
        for key in base['environment']:
            base['environment'][key] += profile['environment'][key]
        for key in base['meeting_type']:
            base['meeting_type'][key] += profile['meeting_type'][key]
        for key in base['weather_preferences']:
            base['weather_preferences'][key] += profile['weather_preferences'][key]
        for key in base['explicit_preferences']:
            base['explicit_preferences'][key] = base['explicit_preferences'].get(key, 0.5) + profile['explicit_preferences'].get(key, 0.5)
        base['activity_level'] += profile['activity_level']
        base['confidence'] += profile['confidence']

    divisor = float(len(profiles) + 1)
    for key in base['features']:
        base['features'][key] = _bounded(base['features'][key] / divisor)
    for key in base['social_style']:
        base['social_style'][key] = _bounded(base['social_style'][key] / divisor)
    for key in base['environment']:
        base['environment'][key] = _bounded(base['environment'][key] / divisor)
    for key in base['meeting_type']:
        base['meeting_type'][key] = _bounded(base['meeting_type'][key] / divisor)
    for key in ['sunny_preference', 'rain_tolerance', 'wind_tolerance']:
        base['weather_preferences'][key] = _bounded(base['weather_preferences'][key] / divisor)
    base['weather_preferences']['temp_target_c'] = max(
        8.0,
        min(30.0, base['weather_preferences']['temp_target_c'] / divisor),
    )
    for key in base['explicit_preferences']:
        base['explicit_preferences'][key] = _bounded(base['explicit_preferences'][key] / divisor)
    base['activity_level'] = _bounded(base['activity_level'] / divisor)
    base['confidence'] = _bounded(base['confidence'] / divisor)

    explicit_conflicts = {'park': 0.0, 'cafe': 0.0, 'restaurant': 0.0}
    if len(profiles) >= 2:
        for key in explicit_conflicts:
            values = [float(item.get('explicit_preferences', {}).get(key, 0.5)) for item in profiles]
            explicit_conflicts[key] = _bounded(max(values) - min(values))
    base['explicit_conflicts'] = explicit_conflicts

    return base


def _aggregate_place_type_weights(participant_vectors=None):
    weights = dict(_DEFAULT_PLACE_WEIGHTS)
    vectors = participant_vectors or []
    if not vectors:
        return weights

    def _delta(value):
        return _bounded(value) - 0.5

    for vec in vectors:
        if not isinstance(vec, dict):
            continue
        nature = _delta(vec.get('interest_nature', 0.5))
        arts = _delta(vec.get('interest_arts', 0.5))
        books = _delta(vec.get('interest_books', 0.5))
        history = _delta(vec.get('interest_history', 0.5))
        sports = _delta(vec.get('interest_sports', 0.5))
        music = _delta(vec.get('interest_music', 0.5))
        small_groups = _delta(vec.get('prefers_small_groups', 0.5))

        # Stronger semantic nudges so topic intent dominates over generic defaults.
        # This keeps "books + history" scenarios focused on library/museum instead of parks.
        weights['park'] += (
            (0.55 * nature)
            + (0.35 * sports)
            - (0.25 * books)
            - (0.22 * history)
            - (0.12 * small_groups)
        )
        weights['library'] += (0.55 * books) + (0.30 * history) + (0.25 * small_groups) - (0.10 * sports)
        weights['museum'] += (0.55 * history) + (0.30 * arts) + (0.15 * books) - (0.08 * sports)
        weights['cafe'] += (0.25 * music) + (0.25 * small_groups) + (0.12 * arts)
        weights['restaurant'] += (0.30 * music) + (0.20 * arts) - (0.08 * small_groups)
        weights['shopping_mall'] += (0.20 * sports) + (0.12 * music) - (0.10 * small_groups)

    # Keep weights in a healthy range to avoid overpowering weather/distance.
    return {
        key: max(0.10, min(2.80, value / max(1, len(vectors))))
        for key, value in weights.items()
    }


def _ranked_place_types(weights, limit=4):
    ranked = sorted(weights.items(), key=lambda item: item[1], reverse=True)
    return [name for name, _ in ranked[:limit]] or ['park']


def _place_type_preference_score(place_types, weights):
    if not place_types:
        return 0.0
    candidates = [weights.get(pt, 0.0) for pt in place_types]
    if not candidates:
        return 0.0
    return max(candidates)

def get_central_point(coordinates):
    if not coordinates:
        return None
    lat_sum = sum(c['lat'] for c in coordinates)
    lng_sum = sum(c['lng'] for c in coordinates)
    count = len(coordinates)
    return {'lat': lat_sum / count, 'lng': lng_sum / count}

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371.0 # Radius of earth in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def fetch_places(location, radius=2000, included_types=None):
    api_key = os.getenv('GOOGLE_MAPS_API_KEY')
    if not api_key:
        print("Warning: GOOGLE_MAPS_API_KEY is not set.")
        return []

    url = "https://places.googleapis.com/v1/places:searchNearby"
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.displayName,places.location,places.types,places.formattedAddress,places.id,places.rating,places.userRatingCount",
        "Content-Type": "application/json"
    }
    data = {
        "includedTypes": included_types or ["park"],
        "maxResultCount": 10,
        "languageCode": "bg",
        "regionCode": "BG",
        "locationRestriction": {
            "circle": {
                "center": {
                    "latitude": location['lat'],
                    "longitude": location['lng']
                },
                "radius": float(radius)
            }
        }
    }
    
    res = requests.post(url, headers=headers, json=data)
    if res.status_code == 200:
        places_data = res.json().get('places', [])
        
        # Map New API response to the format expected by our algorithm
        mapped_places = []
        for p in places_data:
            mapped_places.append({
                'place_id': p.get('id'),
                'name': p.get('displayName', {}).get('text', 'Unknown'),
                'types': p.get('types', []),
                'vicinity': p.get('formattedAddress', ''),
                'rating': p.get('rating', 0),
                'user_ratings_total': p.get('userRatingCount', 0),
                'geometry': {
                    'location': {
                        'lat': p.get('location', {}).get('latitude'),
                        'lng': p.get('location', {}).get('longitude')
                    }
                }
            })
        return mapped_places
    else:
        print("Places API Error:", res.text)
        return []

def fetch_weather(location):
    api_key = os.getenv('OPENWEATHERMAP_API_KEY')
    if not api_key:
        print("Warning: OPENWEATHERMAP_API_KEY is not set.")
        return []
        
    url = f"https://api.openweathermap.org/data/2.5/forecast"
    params = {
        'lat': location['lat'],
        'lon': location['lng'],
        'appid': api_key,
        'units': 'metric'
    }
    res = requests.get(url, params=params)
    if res.status_code == 200:
        return res.json().get('list', [])
    return []


def _weather_midpoint(coordinates):
    # Weather is evaluated at the participant midpoint to reflect shared conditions.
    return get_central_point(coordinates)


def _weather_alignment_score(vh, averaged_profile, place_types):
    prefs = averaged_profile.get('weather_preferences', {})
    sunny_preference = float(prefs.get('sunny_preference', 0.5))
    rain_tolerance = float(prefs.get('rain_tolerance', 0.5))
    wind_tolerance = float(prefs.get('wind_tolerance', 0.5))
    temp_target_c = float(prefs.get('temp_target_c', 20.0))

    temp = float(vh.get('temp') or 20.0)
    wind = float(vh.get('wind') or 0.0)
    weather_main = str(vh.get('weather_main') or '')
    raining = bool(vh.get('rain'))
    outdoor = any(_PLACE_TO_DOMAIN.get(item) == 'outdoor' for item in place_types)

    temp_gap = abs(temp - temp_target_c)
    temp_score = _bounded(1.0 - (temp_gap / 16.0))

    clear_like = weather_main in {'Clear', 'Clouds'}
    sky_score = _bounded((0.45 if clear_like else 0.15) + (0.55 * sunny_preference if clear_like else 0.55 * (1.0 - sunny_preference)))

    rain_score = 1.0
    if raining:
        rain_score = _bounded(0.2 + (0.8 * rain_tolerance))
        if outdoor:
            rain_score = _bounded(rain_score - 0.35)
    else:
        rain_score = _bounded(0.55 + (0.45 * (1.0 - abs(0.5 - rain_tolerance))))

    wind_score = _bounded(1.0 - max(0.0, wind - 3.0) / 12.0)
    if outdoor:
        wind_score = _bounded((0.55 * wind_score) + (0.45 * wind_tolerance))
    else:
        wind_score = _bounded((0.70 * wind_score) + (0.30 * wind_tolerance))

    final_score = _bounded((0.35 * temp_score) + (0.20 * sky_score) + (0.30 * rain_score) + (0.15 * wind_score))
    return final_score

def get_ranked_meetup_spots(
    coordinates,
    participant_vectors=None,
    participant_descriptions=None,
    preferred_time=None,
    top_n=5,
):
    center = get_central_point(coordinates)
    if not center:
        return []

    weather_point = _weather_midpoint(coordinates)
    if not weather_point:
        weather_point = center

    descriptions = participant_descriptions or []
    semantic_profiles = [_semantic_profile_from_description(item) for item in descriptions if str(item or '').strip()]
    semantic_vectors = [_vector_from_semantic_profile(item) for item in semantic_profiles]
    merged_vectors = list(participant_vectors or []) + semantic_vectors

    place_weights = _aggregate_place_type_weights(merged_vectors)
    selected_types = _ranked_place_types(place_weights, limit=3)
    averaged_profile = _average_semantic_profile(semantic_profiles)
    user_similarity = _pair_user_similarity(semantic_profiles)

    places = fetch_places(center, included_types=selected_types)
    weather_forecasts = fetch_weather(weather_point)
    
    # Use the first two forecast dates returned by the API (localized to Bulgaria)
    # to represent the immediate planning horizon (today + tomorrow).
    forecast_dates = []
    for item in weather_forecasts:
        d = datetime.fromtimestamp(item['dt'], tz=_BULGARIA_TZ).date()
        if d not in forecast_dates:
            forecast_dates.append(d)
        if len(forecast_dates) >= 2:
            break
    allowed_dates = set(forecast_dates)

    valid_hours = []
    
    for forecast in weather_forecasts:
        dt = datetime.fromtimestamp(forecast['dt'], tz=_BULGARIA_TZ)
        # Strictly keep suggestions local and near-term: first two forecast days only.
        if dt.date() in allowed_dates and 7 <= dt.hour <= 20:
            valid_hours.append({
                'time': dt,
                'temp': forecast['main']['temp'],
                'rain': forecast.get('rain', {}).get('3h', 0) > 0,
                'wind': forecast['wind']['speed'],
                'weather_main': forecast['weather'][0]['main']
            })
            
    if preferred_time:
        preferred_time_ts = preferred_time.timestamp()
        valid_hours.sort(key=lambda item: abs(item['time'].timestamp() - preferred_time_ts))
        valid_hours = valid_hours[:4]

    if not places or not valid_hours:
        return []

    recommendations = []
        
    for place in places:
        plat = place['geometry']['location']['lat']
        plng = place['geometry']['location']['lng']
        dist = calculate_distance(center['lat'], center['lng'], plat, plng)
        dist_score = _bounded(max(0.0, 2.0 - dist) / 2.0)
        rating = float(place.get('rating') or 0)
        ratings_count = int(place.get('user_ratings_total') or 0)

        rating_score = _bounded(rating / 5.0)
        confidence_score = _bounded(math.log10(max(ratings_count, 1)) / 2.0)
        reviews_score = _bounded((0.72 * rating_score) + (0.28 * confidence_score))
        
        for vh in valid_hours:
            weather_score = _weather_alignment_score(vh, averaged_profile, place.get('types', []))
            time_weight = _time_window_weight(vh['time'])
            context_weather = _bounded((0.75 * weather_score) + (0.25 * time_weight))
            
            types = place.get('types', [])

            preference_score = _place_type_preference_score(types, place_weights)

            # Reward strong type intent alignment and penalize off-topic places.
            rank_bonus = 0.0
            matched_rank = None
            for idx, selected in enumerate(selected_types):
                if selected in types:
                    matched_rank = idx
                    break
            if matched_rank is not None:
                rank_bonus = max(0.0, (len(selected_types) - matched_rank) / max(1, len(selected_types)))

            domain_affinity = _meeting_type_place_affinity(types, averaged_profile)
            park_conflict = float(averaged_profile.get('explicit_conflicts', {}).get('park', 0.0))
            amenities_score = 2 + (preference_score * 6.5) + (rank_bonus * 2.5) + (domain_affinity * 3.2)
            if matched_rank is None:
                amenities_score -= 7
            if not vh['rain']:
                amenities_score += 2
            if park_conflict >= 0.30 and 'park' in types:
                amenities_score -= 4.0
            if park_conflict >= 0.30 and ('cafe' in types or 'coffee_shop' in types):
                amenities_score += 1.2

            place_compatibility = _bounded(min(1.0, (amenities_score / 14.0)))
            graph_place_score = _graph_place_affinity_score(
                types,
                merged_vectors,
                averaged_profile,
            )
            base_score_01 = _bounded(
                (_PLACE_COMPATIBILITY_WEIGHT * place_compatibility)
                + (_WEATHER_WEIGHT * context_weather)
                + (_DISTANCE_WEIGHT * dist_score)
                + (_REVIEWS_WEIGHT * reviews_score)
            )
            total_score_01 = _bounded(
                base_score_01
                + (_GRAPH_PLACE_BONUS_WEIGHT * (graph_place_score - 0.5))
            )
            match_score = int(round(100 * total_score_01))

            explanation = []
            if selected_types:
                top_types_bg = ', '.join(_to_bg_place_type(item) for item in selected_types[:2])
                explanation.append(f"Мястото съвпада с водещите интереси: {top_types_bg}.")
            if domain_affinity >= 0.62:
                explanation.append('Отговаря на предпочитания стил за среща и на двамата.')
            if user_similarity >= 0.70:
                explanation.append('Потребителите имат силно съвпадение в предпочитанията.')
            elif user_similarity < 0.40:
                explanation.append('Има разминаване в предпочитанията; изборът балансира компромис и удобство.')
            if not vh['rain']:
                explanation.append('Метеорологичният прозорец е подходящ за среща.')

            recommendation = {
                'place_name': place.get('name'),
                'place_lat': round(plat, 6),
                'place_lng': round(plng, 6),
                'recommended_time': vh['time'].strftime('%Y-%m-%d %H:00'),
                'recommended_day_bg': '',
                'recommended_date_bg': '',
                'recommended_time_bg': '',
                'recommended_when_bg': '',
                'temperature': round(vh['temp'], 1),
                'weather': _to_bg_weather(vh['weather_main']),
                'score': round(match_score, 2),
                'rating': round(rating, 1),
                'review_count': ratings_count,
                'types': types,
                'amenities': 'Оптимизирано място според интереси, време и контекст',
                'vicinity': place.get('vicinity', ''),
                'preference_score': round(preference_score, 3),
                'selected_types': selected_types,
                'selected_types_bg': [_to_bg_place_type(item) for item in selected_types],
                'user_similarity_score': int(round(100 * user_similarity)),
                'explanation': ' '.join(explanation),
                'score_breakdown': {
                    'place_compatibility': round(place_compatibility, 4),
                    'weather': round(weather_score, 4),
                    'time_window_weight': round(time_weight, 4),
                    'context_weather': round(context_weather, 4),
                    'place_compatibility_weight': _PLACE_COMPATIBILITY_WEIGHT,
                    'weather_weight': _WEATHER_WEIGHT,
                    'distance': round(dist_score, 4),
                    'distance_weight': _DISTANCE_WEIGHT,
                    'reviews': round(reviews_score, 4),
                    'reviews_weight': _REVIEWS_WEIGHT,
                    'user_similarity': round(user_similarity, 4),
                    'user_similarity_weight': 0.0,
                    'graph_place_score': round(graph_place_score, 4),
                    'graph_place_bonus_weight': _GRAPH_PLACE_BONUS_WEIGHT,
                    'base_score': round(base_score_01, 4),
                },
            }
            recommendation.update(_format_meetup_when_bg(vh['time']))
            logger.info('meetup.ranking_decision=%s', recommendation)
            recommendations.append(recommendation)

    recommendations.sort(key=lambda row: float(row.get('score') or 0.0), reverse=True)
    return recommendations[: max(1, int(top_n or 5))]


def get_best_meetup_spot(
    coordinates,
    participant_vectors=None,
    participant_descriptions=None,
    preferred_time=None,
):
    ranked = get_ranked_meetup_spots(
        coordinates,
        participant_vectors=participant_vectors,
        participant_descriptions=participant_descriptions,
        preferred_time=preferred_time,
        top_n=1,
    )
    if not ranked:
        return None
    return ranked[0]
