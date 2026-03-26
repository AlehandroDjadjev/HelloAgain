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

def fetch_places(location, radius=2000):
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
        "includedTypes": ["park"],
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

def get_best_meetup_spot(coordinates):
    center = get_central_point(coordinates)
    if not center:
        return None
        
    places = fetch_places(center)
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

            # Strictly allow only real parks in final candidate scoring.
            if 'park' not in types:
                continue

            # Keep a light preference toward park-friendly weather.
            amenities_score = 6
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
                    'vicinity': place.get('vicinity', '')
                }
                
    return best_match
