"""The wallpaper clock's weather line, fetched by Collie's local server rather than by the page.

The page used to ask ipapi.co (approximate location from the network address) and
api.open-meteo.com (the weather there) itself, from every desktop window and again on every
reload. Asking here gives every window one answer from one cache:

- at most one pair of outside requests per ``REFRESH_S`` (15 minutes) while they succeed;
- after a failure the next attempt waits 1, 2, 4, 8, 16 and then 30 minutes, so an offline
  laptop or a rate-limited service is not asked again every minute;
- a failed refresh keeps the last conditions for up to an hour, marked ``stale``, instead of
  showing them as current, and after that says the weather is unavailable;
- one refresh at a time, run outside the lock and finished within ``DEADLINE_S``: the window that
  starts it waits for it (never longer), and every other window is answered at once from the
  cache, or told the answer is ``pending``.

The off switch is desktop.json's ``{"widgets": {"clock": {"weather": false}}}`` (see
``desktop.weather_enabled``, which also reads an unreadable desktop.json as off). The web handler
reads it before calling ``weather`` so that, when it is off, nothing leaves the machine.
"""
import http.client
import json
import math
import socket
import ssl
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
TIMEOUT_S = 4          # per connection attempt and per read
DEADLINE_S = 8         # the whole refresh, both requests
STUCK_S = 60           # a refresh not back by then (a DNS lookup can outlast any deadline) is given up
MAX_BYTES = 256 * 1024

_lock = threading.Lock()
_local = threading.local()
# checked: monotonic time the last attempt ended; failures: attempts failed since the last success;
# data/updated: the last good answer and the monotonic time it arrived; refreshing/started/
# generation: the one attempt in flight, when it began, and which attempt may still write here.
_state = {"checked": None, "failures": 0, "data": None, "updated": None,
          "refreshing": False, "started": None, "generation": 0}


def _open(request, deadline):
    """Open ``request`` with a watchdog that shuts its sockets at ``deadline`` (monotonic).

    The socket timeout alone bounds each read, not the whole reply: a server sending two bytes a
    second held a refresh for 21 s with TIMEOUT_S at 4. Shutting the socket ends a blocked read on
    every platform (closing it does not on Linux). Returns (response, cancel).
    """
    sockets = []

    class HTTPConnection(http.client.HTTPConnection):
        def connect(self):
            super().connect()
            sockets.append(self.sock)

    class HTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            super().connect()
            sockets.append(self.sock)

    context = ssl.create_default_context()      # what urlopen uses when given none

    class HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(HTTPConnection, req)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(HTTPSConnection, req, context=context)

    def shut():
        for sock in list(sockets):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("no time left for the weather request")
    watchdog = threading.Timer(remaining, shut)
    watchdog.daemon = True
    watchdog.start()
    try:
        opener = urllib.request.build_opener(HTTPHandler, HTTPSHandler)
        response = opener.open(request, timeout=min(TIMEOUT_S, remaining))
    except BaseException:
        watchdog.cancel()
        raise
    return response, watchdog.cancel


def _get(url):
    deadline = getattr(_local, "deadline", None)
    if deadline is None:
        deadline = time.monotonic() + DEADLINE_S
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/json"})
    response, cancel = _open(request, deadline)
    try:
        raw = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("the weather reply took too long")
            chunk = response.read1(65536)          # what has arrived; never waits to fill a buffer
            if not chunk:
                break
            raw += chunk
            if len(raw) > MAX_BYTES:
                raise ValueError("weather response too large")
    finally:
        cancel()
        response.close()
    return json.loads(bytes(raw))


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
    if _state.get("refreshing"):
        return {"ok": False, "pending": True}   # the first answer is on its way; ask again shortly
    return {"ok": False, "error": "Weather is unavailable right now."}


def _refresh(generation, deadline):
    """One attempt, run on its own thread with the lock released while it waits on the network."""
    _local.deadline = deadline
    try:
        data = _fetch()
    except Exception:          # noqa: BLE001 - whatever went wrong, the attempt failed and backs off
        data = None
    finally:
        _local.deadline = None
    with _lock:
        if _state.get("generation", 0) != generation:
            return             # given up on as stuck; a newer attempt owns the cache now
        if data is not None:
            _state.update(data=data, updated=time.monotonic(), failures=0)
        else:
            _state["failures"] += 1
        _state.update(checked=time.monotonic(), refreshing=False)


def weather():
    """The current conditions for the desktop clock, from the shared cache when it is fresh.

    Returns the conditions with ``stale`` (True when they are not current: the latest refresh
    failed, or they are older than a refresh interval); ``{"ok": False, "pending": True}`` while
    the first answer is being fetched; or ``{"ok": False, "error": ...}`` when there is nothing
    less than an hour old to show.
    """
    with _lock:
        now = time.monotonic()
        if _state.get("refreshing") and now - (_state.get("started") or now) >= STUCK_S:
            # It never came back. Count it as failed; if it finishes later it writes nothing.
            _state.update(generation=_state.get("generation", 0) + 1, refreshing=False,
                          checked=now, failures=_state["failures"] + 1)
        start = not _state.get("refreshing") and _due(now, time.time())
        if start:
            generation = _state.get("generation", 0) + 1
            deadline = now + DEADLINE_S
            _state.update(refreshing=True, started=now, generation=generation)
    if start:
        worker = threading.Thread(target=_refresh, args=(generation, deadline),
                                  name="collie-desktop-weather", daemon=True)
        worker.start()
        # The window that started the refresh waits for it, never past the deadline; every other
        # window got past the lock above at once and is answered from the cache below.
        worker.join(DEADLINE_S + 1)
    with _lock:
        return _answer(time.monotonic(), time.time())
