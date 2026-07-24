"""
weather.current — current conditions + short forecast via wttr.in (no API key).
Falls back to a web search snippet if the API is unreachable.
"""

import json
import urllib.parse
import urllib.request

from kai.tools.registry import registry


@registry.tool(
    name="weather.current",
    description=(
        "Get current weather plus a short forecast (today + next 2 days). "
        "Defaults to the user's location; pass `location` for any city. "
        "PRIVACY: with no location this sends the user's IP to wttr.in for geolocation."
    ),
    parameters={
        "location": {
            "type": "string",
            "description": "Optional city or place (e.g. 'London', 'Paris, France'). Leave empty for here.",
        },
    },
)
def get_weather(location: str = "") -> str:
    location = (location or "").strip()
    path = urllib.parse.quote(location) if location else ""

    # Try wttr.in JSON API (HTTP — HTTPS times out on some Windows configs)
    try:
        req = urllib.request.Request(
            f"http://wttr.in/{path}?format=j1",
            headers={"User-Agent": "curl/7.68.0"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        return _format_wttr(data)
    except Exception:
        pass

    # Fallback: search DuckDuckGo for "weather"
    try:
        from kai.tools.web.search import _ddg_search

        q = f"weather {location}" if location else "current weather conditions"
        results = _ddg_search(q, max_results=1)
        if results:
            return f"Weather (via search):\n{results[0]['snippet']}"
    except Exception:
        pass

    return "Weather data unavailable — check your internet connection."


def _format_wttr(data: dict) -> str:
    cur = data["current_condition"][0]
    area = data.get("nearest_area", [{}])[0]
    city = area.get("areaName", [{}])[0].get("value", "Unknown")
    region = area.get("region", [{}])[0].get("value", "")
    location = f"{city}, {region}" if region else city

    desc = cur["weatherDesc"][0]["value"]
    temp_f = cur["temp_F"]
    temp_c = cur["temp_C"]
    feels_f = cur["FeelsLikeF"]
    feels_c = cur["FeelsLikeC"]
    humidity = cur["humidity"]
    wind_mph = cur["windspeedMiles"]
    wind_dir = cur["winddir16Point"]
    visibility = cur.get("visibility", "?")

    out = (
        f"{location}\n"
        f"Now: {desc}, {temp_f}°F / {temp_c}°C (feels {feels_f}°F / {feels_c}°C)\n"
        f"Humidity {humidity}% · Wind {wind_mph} mph {wind_dir} · Visibility {visibility} mi"
    )

    forecast = _format_forecast(data.get("weather", []))
    if forecast:
        out += "\n\nForecast:\n" + forecast
    return out


def _format_forecast(days: list[dict]) -> str:
    """Compact 'today + next 2 days' block from the wttr forecast array
    (already in the j1 response — was previously discarded)."""
    lines = []
    for i, day in enumerate(days[:3]):
        label = ("Today", "Tomorrow", "Day after")[i] if i < 3 else day.get("date", "")
        hi_f, lo_f = day.get("maxtempF", "?"), day.get("mintempF", "?")
        hi_c, lo_c = day.get("maxtempC", "?"), day.get("mintempC", "?")

        hourly = day.get("hourly", []) or []
        # Midday entry (~noon) is the most representative single condition.
        midday = hourly[len(hourly) // 2] if hourly else {}
        cond = (midday.get("weatherDesc", [{}]) or [{}])[0].get("value", "").strip()
        rain = max((int(h.get("chanceofrain", 0) or 0) for h in hourly), default=0)

        bits = [f"{label}: {lo_f}-{hi_f}°F / {lo_c}-{hi_c}°C"]
        if cond:
            bits.append(cond)
        if rain:
            bits.append(f"{rain}% rain")
        lines.append("  " + ", ".join(bits))
    return "\n".join(lines)
