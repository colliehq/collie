"""The wallpaper clock's weather line, fetched by Collie's local server rather than by the page.

The page used to ask ipapi.co (approximate location from the network address) and
api.open-meteo.com (the weather there) itself, from every desktop window and again on every
reload. Asking here gives every window one answer from one cache:

- at most one pair of outside requests per ``REFRESH_S`` (15 minutes) while they succeed;
- after a failure the next attempt waits 1, 2, 4, 8, 16 and then 30 minutes, so an offline
  laptop or a rate-limited service is not asked again every minute;
- a failed refresh keeps the last conditions for up to an hour, marked ``stale``, instead of
  showing them as current, and after that says the weather is unavailable.

The off switch is desktop.json's ``"clock": {"weather": false}`` (see
``desktop.weather_enabled``). The web handler reads it before calling ``weather`` so that, when it
is off, nothing leaves the machine.
"""
import http.client
import json
import math
import threading
import time
import urllib.parse
import urllib.request

GEO_URL = "https://ipapi.co/json/"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# Stated in docs/privacy.md. Python's default "Python-urllib/x.y" is refused by some CDNs, and a
# plain product name says who is asking without saying anything about the person.
USER_AGENT = "collie-desktop-weather (+https://github.com/colliehq/collie)"
REFRESH_S = 15 * 60
RETRY_FIRST_S = 60
RETRY_MAX_S = 30 * 60
STALE_LIMIT_S = 60 * 60
TIMEOUT_S = 4
MAX_BYTES = 256 * 1024

_lock = threading.Lock()
# checked: monotonic time of the last attempt; failures: attempts failed since the last success;
# data/updated: the last good answer and the monotonic time it arrived.
_state = {"checked": None, "failures": 0, "data": None, "updated": None}


def _get(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("weather response too large")
    return json.loads(raw)


def _number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("missing weather measurement")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("weather measurement out of range")
    return value


def retry_delay(failures):
    """Seconds to wait after ``failures`` consecutive failed attempts (1 → 60 s, doubling, capped)."""
    if failures <= 0:
        return REFRESH_S
    return min(RETRY_FIRST_S * 2 ** (failures - 1), RETRY_MAX_S)


def _fetch():
    geo = _get(GEO_URL)
    params = {
        "latitude": _number(geo.get("latitude"), -90, 90),
        "longitude": _number(geo.get("longitude"), -180, 180),
        "current": "temperature_2m,weather_code,is_day",
        "temperature_unit": "celsius", "timezone": "auto",
    }
    forecast = _get(FORECAST_URL + "?" + urllib.parse.urlencode(params))
    current = forecast.get("current") or {}
    code = _number(current.get("weather_code"), 0, 99)
    is_day = current.get("is_day")
    city = geo.get("city")
    country = geo.get("country_code")
    return {
        "ok": True,
        "temp_c": _number(current.get("temperature_2m"), -100, 70),
        "code": int(code),
        # 1 day, 0 night; anything else is "not said", and the page then draws the day icon.
        "is_day": is_day if is_day in (0, 1) and not isinstance(is_day, bool) else None,
        "city": city.strip()[:120] if isinstance(city, str) else "",
        "country_code": country.strip().upper()[:2] if isinstance(country, str) else "",
        "observed_at": str(current.get("time") or "")[:40],
        "fetched_at": int(time.time()),
        "source": "Open-Meteo",
    }


def weather():
    """The current conditions for the desktop clock, from the shared cache when it is fresh.

    Returns the conditions with ``stale`` (True when the latest refresh failed), or
    ``{"ok": False, "error": ...}`` when there is nothing recent enough to show.
    """
    with _lock:
        now = time.monotonic()
        last = _state["checked"]
        if last is None or now - last >= retry_delay(_state["failures"]):
            try:
                data = _fetch()
            except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
                _state["failures"] += 1
            else:
                _state.update(data=data, updated=time.monotonic(), failures=0)
            _state["checked"] = time.monotonic()
        data = _state["data"]
        if data is not None and time.monotonic() - _state["updated"] < STALE_LIMIT_S:
            return dict(data, stale=_state["failures"] > 0)
        return {"ok": False, "error": "Weather is unavailable right now."}
