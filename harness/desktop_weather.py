"""The wallpaper clock's weather line, fetched by Collie's local server rather than by the page.

The page used to ask ipapi.co (approximate location from the network address) and
api.open-meteo.com (the weather there) itself, from every desktop window and again on every
reload. Asking here gives every window one answer from one cache:

- at most one pair of outside requests per ``REFRESH_S`` (15 minutes) while they succeed;
- after a failure the next attempt waits 1, 2, 4, 8, 16 and then 30 minutes, so an offline
  laptop or a rate-limited service is not asked again every minute;
- a failed refresh keeps the last conditions for up to an hour, marked ``stale``, instead of
  showing them as current, and after that says the weather is unavailable.

The off switch is desktop.json's ``{"widgets": {"clock": {"weather": false}}}`` (see
``desktop.weather_enabled``, which also reads an unreadable desktop.json as off). The web handler
reads it before calling ``weather`` so that, when it is off, nothing leaves the machine.
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


# How old an answer is comes from the wall clock (its fetched_at). time.monotonic() does not
# advance while macOS or Linux sleeps, so judged by it alone a 9-hour-old answer from before an
# overnight sleep was served as current, moon and all. The monotonic clock still spaces retries,
# and it bounds an answer's life too, in case the wall clock is set back.
def _age(data, wall):
    at = data.get("fetched_at") if data else None
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return wall - at


def _due(now, wall):
    last = _state["checked"]
    if last is None or now - last >= retry_delay(_state["failures"]):
        return True
    if _state["failures"]:
        return False                              # after a failure, only the backoff decides
    age = _age(_state["data"], wall)
    return age is None or age < 0 or age >= REFRESH_S


def _answer(now, wall):
    data = _state["data"]
    age = _age(data, wall)
    held = now - (_state["updated"] if _state["updated"] is not None else now)
    if age is not None and age < STALE_LIMIT_S and held < STALE_LIMIT_S:
        # Stale: the latest refresh failed, or the answer is older than a refresh interval (it
        # outlived a sleep), or its age cannot be told because the wall clock went back.
        return dict(data, stale=_state["failures"] > 0 or age < 0 or age >= REFRESH_S)
    return {"ok": False, "error": "Weather is unavailable right now."}


def weather():
    """The current conditions for the desktop clock, from the shared cache when it is fresh.

    Returns the conditions with ``stale`` (True when they are not current: the latest refresh
    failed, or they are older than a refresh interval), or ``{"ok": False, "error": ...}`` when
    there is nothing less than an hour old to show.
    """
    with _lock:
        if _due(time.monotonic(), time.time()):
            try:
                data = _fetch()
            except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
                _state["failures"] += 1
            else:
                _state.update(data=data, updated=time.monotonic(), failures=0)
            _state["checked"] = time.monotonic()
        return _answer(time.monotonic(), time.time())
