import os
import requests
import math
from datetime import datetime, timedelta

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
        "X-Goog-FieldMask": "places.displayName,places.location,places.types,places.formattedAddress,places.id",
        "Content-Type": "application/json"
    }
    data = {
        "includedTypes": ["park", "cafe", "tourist_attraction"],
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
        
        for vh in valid_hours:
            weather_score = 10
            if vh['rain']:
                weather_score -= 10
            if vh['temp'] < 10 or vh['temp'] > 30:
                weather_score -= 5
            if vh['wind'] > 10:
                weather_score -= 3
            
            types = place.get('types', [])
            amenities_score = 0
            if 'park' in types and not vh['rain']:
                amenities_score += 5
            elif 'cafe' in types or 'restaurant' in types:
                amenities_score += 5
                if vh['rain']:
                    amenities_score += 10
                    
            total_score = dist_score + weather_score + amenities_score
            
            if total_score > best_score:
                best_score = total_score
                amenities_desc = "Indoor (Cafe/Restaurant)" if ('cafe' in types or 'restaurant' in types) else "Outdoor (Park)"
                best_match = {
                    'place_name': place.get('name'),
                    'place_lat': plat,
                    'place_lng': plng,
                    'recommended_time': vh['time'].strftime('%Y-%m-%d %H:00'),
                    'temperature': vh['temp'],
                    'weather': vh['weather_main'],
                    'score': total_score,
                    'types': types,
                    'amenities': amenities_desc,
                    'vicinity': place.get('vicinity', '')
                }
                
    return best_match
