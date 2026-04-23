from __future__ import annotations

from datetime import datetime
from typing import Any

import requests


_WEATHER_CODE_LABELS = {
    0: "Ясно",
    1: "Предимно ясно",
    2: "Частично облачно",
    3: "Облачно",
    45: "Мъгла",
    48: "Скрежна мъгла",
    51: "Лек ръмеж",
    53: "Ръмеж",
    55: "Силен ръмеж",
    56: "Леден ръмеж",
    57: "Силен леден ръмеж",
    61: "Лек дъжд",
    63: "Дъжд",
    65: "Силен дъжд",
    66: "Леден дъжд",
    67: "Силен леден дъжд",
    71: "Слаб сняг",
    73: "Сняг",
    75: "Силен сняг",
    77: "Снежни зърна",
    80: "Кратки валежи",
    81: "Валежи",
    82: "Силни валежи",
    85: "Слаб снеговалеж",
    86: "Силен снеговалеж",
    95: "Гръмотевици",
    96: "Гръмотевици с градушка",
    99: "Силни гръмотевици с градушка",
}


def _icon_key(weather_code: int, is_day: bool) -> str:
    if weather_code == 0:
        return "sun" if is_day else "moon"
    if weather_code in {1, 2, 3}:
        return "cloud_sun" if is_day else "cloud_moon"
    if weather_code in {45, 48}:
        return "fog"
    if weather_code in {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}:
        return "rain"
    if weather_code in {71, 73, 75, 77, 85, 86}:
        return "snow"
    if weather_code in {95, 96, 99}:
        return "storm"
    return "cloud"


def _advice(weather_code: int, temperature_c: float, is_day: bool) -> str:
    if temperature_c >= 30:
        return "Днес е доста топло. Носете вода и стойте повече на сянка."
    if temperature_c <= 2:
        return "Навън е студено. Облечете се по-топло."
    if weather_code in {61, 63, 65, 80, 81, 82}:
        return "Възможен е дъжд. Добре е да вземете чадър."
    if weather_code in {71, 73, 75, 77, 85, 86}:
        return "Навън е зимно. Внимавайте по хлъзгавите места."
    if weather_code in {95, 96, 99}:
        return "По-добре останете на закрито, докато бурята отмине."
    if not is_day:
        return "Вечерта изглежда спокойна и е подходяща за кратка разходка."
    return "Времето е приятно за излизане навън."


def _weather_label(weather_code: int) -> str:
    return _WEATHER_CODE_LABELS.get(weather_code, "Спокойно време")


def get_current_weather_snapshot(
    *,
    lat: float,
    lng: float,
    timezone_name: str | None = None,
) -> dict[str, Any]:
    params = {
        "latitude": lat,
        "longitude": lng,
        "current": ",".join(
            [
                "temperature_2m",
                "apparent_temperature",
                "weather_code",
                "is_day",
                "wind_speed_10m",
            ]
        ),
        "timezone": timezone_name or "auto",
        "forecast_days": 1,
    }
    response = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params=params,
        timeout=12,
    )
    response.raise_for_status()
    payload = response.json()
    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    current_units = (
        payload.get("current_units")
        if isinstance(payload.get("current_units"), dict)
        else {}
    )

    weather_code = int(current.get("weather_code") or 0)
    is_day = bool(int(current.get("is_day") or 0))
    temperature_c = float(current.get("temperature_2m") or 0.0)
    apparent_temperature_c = float(current.get("apparent_temperature") or temperature_c)
    wind_speed = float(current.get("wind_speed_10m") or 0.0)
    observed_at = str(current.get("time") or datetime.utcnow().isoformat())
    label = _weather_label(weather_code)
    summary = f"{label}, {round(temperature_c)}°C"

    return {
        "widget_type": "weather_snapshot",
        "title": "Времето сега",
        "summary": summary,
        "message": "Текущото време е готово.",
        "surface_preference": "popup_only",
        "board_object": {
            "tags": [
                "kind:weather_snapshot",
                "source:weather",
                "entity:weather_snapshot",
            ],
            "extra_data": {
                "kind": "weather_snapshot",
                "summary": summary,
                "icon_key": _icon_key(weather_code, is_day),
            },
        },
        "weather": {
            "label": label,
            "summary": summary,
            "weather_code": weather_code,
            "temperature_c": round(temperature_c, 1),
            "apparent_temperature_c": round(apparent_temperature_c, 1),
            "wind_speed": round(wind_speed, 1),
            "wind_unit": str(current_units.get("wind_speed_10m") or "km/h"),
            "temperature_unit": str(current_units.get("temperature_2m") or "°C"),
            "icon_key": _icon_key(weather_code, is_day),
            "is_day": is_day,
            "advice": _advice(weather_code, temperature_c, is_day),
            "observed_at": observed_at,
            "timezone": str(payload.get("timezone") or timezone_name or ""),
            "latitude": round(float(lat), 6),
            "longitude": round(float(lng), 6),
        },
    }
