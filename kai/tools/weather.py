"""
weather.current — current conditions via wttr.in (no API key).
Falls back to a web search snippet if the API is unreachable.
"""
import json
import urllib.request
import urllib.parse

from kai.tools.registry import registry


@registry.tool(
    name="weather.current",
    description=(
        "Get the current weather conditions. Returns temperature, description, "
        "humidity, wind speed, and feels-like temp. No location needed — uses your IP."
    ),
)
def get_weather() -> str:
    # Try wttr.in JSON API (HTTP — HTTPS times out on some Windows configs)
    try:
        req = urllib.request.Request(
            "http://wttr.in/?format=j1",
            headers={"User-Agent": "curl/7.68.0"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        return _format_wttr(data)
    except Exception:
        pass

    # Fallback: search DuckDuckGo for "weather"
    try:
        from kai.tools.search import _ddg_search
        results = _ddg_search("current weather conditions", max_results=1)
        if results:
            return f"Weather (via search):\n{results[0]['snippet']}"
    except Exception:
        pass

    return "Weather data unavailable — check your internet connection."


def _format_wttr(data: dict) -> str:
    cur  = data["current_condition"][0]
    area = data.get("nearest_area", [{}])[0]
    city = area.get("areaName", [{}])[0].get("value", "Unknown")
    region = area.get("region", [{}])[0].get("value", "")
    location = f"{city}, {region}" if region else city

    desc      = cur["weatherDesc"][0]["value"]
    temp_f    = cur["temp_F"]
    temp_c    = cur["temp_C"]
    feels_f   = cur["FeelsLikeF"]
    feels_c   = cur["FeelsLikeC"]
    humidity  = cur["humidity"]
    wind_mph  = cur["windspeedMiles"]
    wind_dir  = cur["winddir16Point"]
    visibility = cur.get("visibility", "?")

    return (
        f"{location}\n"
        f"{desc}, {temp_f}°F / {temp_c}°C\n"
        f"Feels like {feels_f}°F / {feels_c}°C\n"
        f"Humidity {humidity}% · Wind {wind_mph} mph {wind_dir} · Visibility {visibility} mi"
    )
