import os
import requests
import math
from datetime import datetime, timedelta


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


def _to_bg_weather(label):
    if not label:
        return ''
    return _WEATHER_BG_MAP.get(label, label)


_DEFAULT_PLACE_WEIGHTS = {
    'park': 1.0,
    'cafe': 0.75,
    'library': 0.68,
    'museum': 0.62,
    'restaurant': 0.70,
    'shopping_mall': 0.45,
}


def _bounded(value):
    return max(0.0, min(1.0, float(value)))


def _aggregate_place_type_weights(participant_vectors=None):
    weights = dict(_DEFAULT_PLACE_WEIGHTS)
    vectors = participant_vectors or []
    if not vectors:
        return weights

    for vec in vectors:
        if not isinstance(vec, dict):
            continue
        nature = _bounded(vec.get('interest_nature', 0.5))
        arts = _bounded(vec.get('interest_arts', 0.5))
        books = _bounded(vec.get('interest_books', 0.5))
        history = _bounded(vec.get('interest_history', 0.5))
        sports = _bounded(vec.get('interest_sports', 0.5))
        music = _bounded(vec.get('interest_music', 0.5))
        small_groups = _bounded(vec.get('prefers_small_groups', 0.5))

        # Additive nudges on top of defaults so missing preferences still behave sensibly.
        weights['park'] += 0.45 * nature + 0.25 * sports
        weights['library'] += 0.35 * books + 0.20 * small_groups
        weights['museum'] += 0.30 * history + 0.20 * arts
        weights['cafe'] += 0.20 * music + 0.25 * small_groups
        weights['restaurant'] += 0.18 * music + 0.16 * arts
        weights['shopping_mall'] += 0.12 * sports

    # Keep weights in a healthy range to avoid overpowering weather/distance.
    return {
        key: max(0.30, min(2.50, value / max(1, len(vectors))))
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

def get_best_meetup_spot(coordinates, participant_vectors=None, preferred_time=None):
    center = get_central_point(coordinates)
    if not center:
        return None

    place_weights = _aggregate_place_type_weights(participant_vectors)
    selected_types = _ranked_place_types(place_weights)

    places = fetch_places(center, included_types=selected_types)
    weather_forecasts = fetch_weather(center)
    
    now = datetime.now()
    valid_hours = []
    
    for forecast in weather_forecasts:
        dt = datetime.fromtimestamp(forecast['dt'])
        # Looking for today or tomorrow between 09:00 and 18:00
        if 9 <= dt.hour <= 18:
            valid_hours.append({
                'time': dt,
                'temp': forecast['main']['temp'],
                'rain': forecast.get('rain', {}).get('3h', 0) > 0,
                'wind': forecast['wind']['speed'],
                'weather_main': forecast['weather'][0]['main']
            })
            
    if preferred_time:
        valid_hours.sort(key=lambda item: abs((item['time'] - preferred_time).total_seconds()))
        valid_hours = valid_hours[:4]

    best_score = -9999
    best_match = None
    
    if not places or not valid_hours:
        return None
        
    for place in places:
        plat = place['geometry']['location']['lat']
        plng = place['geometry']['location']['lng']
        dist = calculate_distance(center['lat'], center['lng'], plat, plng)
        dist_score = max(0, 2 - dist) * 10 
        rating = float(place.get('rating') or 0)
        ratings_count = int(place.get('user_ratings_total') or 0)

        # Weight quality by review score and confidence by number of reviews.
        rating_score = min(10.0, max(0.0, rating) * 2.0)
        confidence_score = min(5.0, math.log10(max(ratings_count, 1)) * 2.5)
        reviews_score = rating_score + confidence_score
        
        for vh in valid_hours:
            weather_score = 10
            if vh['rain']:
                weather_score -= 10
            if vh['temp'] < 10 or vh['temp'] > 30:
                weather_score -= 5
            if vh['wind'] > 10:
                weather_score -= 3
            
            types = place.get('types', [])

            preference_score = _place_type_preference_score(types, place_weights)

            amenities_score = 6 + (preference_score * 2.5)
            if not vh['rain']:
                amenities_score += 2
                    
            total_score = dist_score + weather_score + amenities_score + reviews_score
            
            if total_score > best_score:
                best_score = total_score
                amenities_desc = "Открито (Парк)"
                best_match = {
                    'place_name': place.get('name'),
                    'place_lat': round(plat, 6),
                    'place_lng': round(plng, 6),
                    'recommended_time': vh['time'].strftime('%Y-%m-%d %H:00'),
                    'temperature': round(vh['temp'], 1),
                    'weather': _to_bg_weather(vh['weather_main']),
                    'score': round(total_score, 2),
                    'rating': round(rating, 1),
                    'review_count': ratings_count,
                    'types': types,
                    'amenities': amenities_desc,
                    'vicinity': place.get('vicinity', ''),
                    'preference_score': round(preference_score, 3),
                    'selected_types': selected_types,
                }
                
    return best_match
