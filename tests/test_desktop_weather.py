"""The wallpaper weather is asked for by Collie's server, once for every desktop window.

Nothing here reaches ipapi.co or Open-Meteo: `_get` (or urlopen itself) is replaced, and the
module's clocks are a stand-in the tests move by hand.
"""
import concurrent.futures
import json
import threading
import urllib.error

import pytest

from harness import desktop
from harness import desktop_weather as wx

CURRENT = {"temperature_2m": 16.6, "weather_code": 1, "is_day": 0, "time": "2026-09-11T03:00"}
GEO = {"latitude": 37.3, "longitude": -121.9, "city": "San Jose", "country_code": "US"}


class Clock:
    """Stands in for the time module inside desktop_weather. Awake, monotonic() and time() move
    together; asleep, only the wall clock moves, which is what macOS and Linux do."""

    def __init__(self):
        self.mono, self.wall = 1000.0, 1_790_000_000.0

    def monotonic(self):
        return self.mono

    def time(self):
        return self.wall

    def advance(self, seconds):
        self.mono += seconds
        self.wall += seconds

    def sleep(self, seconds):
        self.wall += seconds


@pytest.fixture
def weather_io(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(wx, "_state", {"checked": None, "failures": 0, "data": None,
                                       "updated": None})
    monkeypatch.setattr(wx, "time", clock)
    calls = []
    current = dict(CURRENT)

    def get(url):
        calls.append(url)
        if url.startswith("https://ipapi.co/"):
            return dict(GEO)
        return {"current": dict(current)}

    monkeypatch.setattr(wx, "_get", get)
    return clock, calls, current, get


def test_concurrent_windows_share_one_fetch_and_get_their_own_copy(weather_io):
    _, calls, _, _ = weather_io
    barrier = threading.Barrier(6)

    def call(_):
        barrier.wait()
        return wx.weather()

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(call, range(6)))
    assert len(calls) == 2, calls                       # one location lookup, one forecast
    assert all(row == rows[0] for row in rows)
    assert rows[0]["ok"] is True and rows[0]["temp_c"] == 16.6
    assert rows[0]["code"] == 1 and rows[0]["is_day"] == 0 and rows[0]["stale"] is False
    assert rows[0]["city"] == "San Jose" and rows[0]["country_code"] == "US"
    rows[0]["city"] = "mutated"
    assert wx.weather()["city"] == "San Jose"
    assert len(calls) == 2


def test_the_cache_is_kept_for_fifteen_minutes(weather_io):
    clock, calls, _, _ = weather_io
    wx.weather()
    clock.advance(wx.REFRESH_S - 1)
    wx.weather()
    assert len(calls) == 2
    clock.advance(1)
    wx.weather()
    assert len(calls) == 4


def test_a_failure_keeps_recent_conditions_marked_stale_then_expires_and_recovers(
        weather_io, monkeypatch):
    clock, calls, _, get = weather_io
    wx.weather()
    clock.advance(wx.REFRESH_S)

    def offline(url):
        calls.append(url)
        raise urllib.error.URLError("private network detail")

    monkeypatch.setattr(wx, "_get", offline)
    stale = wx.weather()
    assert stale["stale"] is True and stale["temp_c"] == 16.6
    assert "private network detail" not in json.dumps(stale)
    clock.advance(wx.STALE_LIMIT_S)
    gone = wx.weather()
    assert gone["ok"] is False and "temp_c" not in gone
    assert "private network detail" not in json.dumps(gone)
    clock.advance(wx.RETRY_MAX_S)
    monkeypatch.setattr(wx, "_get", get)
    back = wx.weather()
    assert back["ok"] is True and back["stale"] is False


def test_after_a_night_asleep_the_old_answer_is_refreshed_not_served_as_current(weather_io):
    clock, calls, current, _ = weather_io
    night = wx.weather()
    assert night["is_day"] == 0 and night["stale"] is False
    clock.advance(10 * 60)
    clock.sleep(9 * 3600)                      # the lid closes: monotonic stops, the wall clock goes on
    current.update(is_day=1, temperature_2m=12.0)
    morning = wx.weather()
    assert len(calls) == 4                     # asked again on waking, not 5 minutes later
    assert morning["is_day"] == 1 and morning["temp_c"] == 12.0 and morning["stale"] is False


def test_asleep_then_offline_the_old_answer_is_dropped_after_an_hour_of_wall_time(
        weather_io, monkeypatch):
    clock, _, _, _ = weather_io
    wx.weather()
    clock.advance(10 * 60)
    clock.sleep(9 * 3600)

    def offline(url):
        raise OSError("network is unreachable")

    monkeypatch.setattr(wx, "_get", offline)
    woke = wx.weather()                        # 9 hours old: not shown at all
    assert woke["ok"] is False and "temp_c" not in woke


def test_a_short_sleep_marks_the_answer_stale_until_it_is_refreshed(weather_io, monkeypatch):
    clock, _, _, _ = weather_io
    wx.weather()
    clock.sleep(20 * 60)

    def offline(url):
        raise OSError("network is not up yet")

    monkeypatch.setattr(wx, "_get", offline)
    woke = wx.weather()
    assert woke["ok"] is True and woke["stale"] is True    # 20 minutes old: kept, but not current


def test_a_wall_clock_set_back_makes_the_answer_stale(weather_io, monkeypatch):
    clock, calls, _, _ = weather_io
    wx.weather()
    clock.wall -= 3600

    def offline(url):
        calls.append(url)
        raise OSError("offline")

    monkeypatch.setattr(wx, "_get", offline)
    back = wx.weather()
    assert len(calls) == 3 and back["stale"] is True      # its age cannot be told: refresh, stale
    clock.advance(wx.STALE_LIMIT_S)                        # awake for an hour by monotonic time
    assert wx.weather()["ok"] is False


def test_failures_back_off_exponentially_up_to_thirty_minutes(weather_io, monkeypatch):
    clock, _, _, _ = weather_io
    attempts = []

    def offline(url):
        attempts.append(clock.mono)
        raise OSError("offline")

    monkeypatch.setattr(wx, "_get", offline)
    start = clock.mono
    for _ in range(4 * 3600):              # four hours, asked every second by some window
        wx.weather()
        clock.advance(1)
    gaps = [b - a for a, b in zip(attempts, attempts[1:])]
    assert gaps[:6] == [60, 120, 240, 480, 960, 1800], gaps
    assert set(gaps[5:]) == {1800}
    # Four hours of a window asking every second reached the outside 12 times, not 14,400.
    assert len(attempts) == 12 and attempts[0] == start


@pytest.mark.parametrize("bad", [None, True, float("nan"), float("inf"), "20", -200])
def test_an_invalid_measurement_is_not_shown_as_a_temperature(weather_io, bad):
    _, _, current, _ = weather_io
    current["temperature_2m"] = bad
    assert wx.weather()["ok"] is False


@pytest.mark.parametrize("geo", [{"error": True, "reason": "RateLimited"}, [], "nope",
                                 {"latitude": 91, "longitude": 0}])
def test_a_location_answer_without_coordinates_asks_no_forecast(weather_io, monkeypatch, geo):
    _, calls, _, _ = weather_io

    def get(url):
        calls.append(url)
        return geo

    monkeypatch.setattr(wx, "_get", get)
    assert wx.weather()["ok"] is False
    assert all(url.startswith("https://ipapi.co/") for url in calls)


def test_requests_name_collie_and_refuse_an_oversized_answer(monkeypatch):
    seen = []

    class Response:
        def __init__(self, body):
            self.body = body

        def read(self, limit):
            return self.body[:limit]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def urlopen(request, timeout):
        seen.append((request.full_url, request.get_header("User-agent"), timeout))
        return Response(b"x" * (wx.MAX_BYTES + 1))

    monkeypatch.setattr(wx.urllib.request, "urlopen", urlopen)
    with pytest.raises(ValueError):
        wx._get(wx.GEO_URL)
    assert seen == [(wx.GEO_URL, wx.USER_AGENT, wx.TIMEOUT_S)]
    assert "collie" in wx.USER_AGENT.lower() and "urllib" not in wx.USER_AGENT.lower()


def test_the_forecast_request_carries_only_the_rounded_point(weather_io):
    _, calls, _, _ = weather_io
    wx.weather()
    forecast = calls[1]
    assert forecast.startswith("https://api.open-meteo.com/v1/forecast?")
    assert "latitude=37.3" in forecast and "longitude=-121.9" in forecast
    assert "San" not in forecast and "US" not in forecast


@pytest.mark.parametrize("saved, enabled", [
    (None, True),                                                     # no desktop.json
    ({"widgets": {"clock": {"slot": "bl"}}}, True),
    ({"widgets": {"clock": {"weather": False}}}, False),
    ({"widgets": {"clock": {"on": False}}}, False),                  # no clock, no weather line
    ({"widgets": {"clock": {"weather": 0}}}, True),                  # only false turns it off
    ("{not json", False),                            # there but unreadable: off, never the default
    ([], False),
])
def test_weather_enabled_reads_the_clock_switch(tmp_path, monkeypatch, saved, enabled):
    path = tmp_path / "desktop.json"
    monkeypatch.setattr(desktop, "CONFIG_PATH", str(path))
    seeded = []
    monkeypatch.setattr(desktop, "_seed_apps", lambda: seeded.append(1) or [])
    if isinstance(saved, str):
        path.write_text(saved, encoding="utf-8")
    elif saved is not None:
        path.write_text(json.dumps(saved), encoding="utf-8")
    assert desktop.weather_enabled() is enabled
    assert seeded == []                  # a weather poll never walks the Start Menu
