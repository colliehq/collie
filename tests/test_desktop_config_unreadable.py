"""A desktop.json that says the weather is off keeps it off, even when it cannot be parsed.

An unparseable desktop.json used to mean "use the defaults", and the defaults have the weather on.
A file saying "weather": false behind a UTF-8 byte-order mark (what Windows PowerShell 5.1 writes
with -Encoding utf8) or with one trailing comma therefore turned the weather back on, and both the
page and the server asked ipapi.co and Open-Meteo. The 0.30.0 notes also described the switch as a
top-level "clock" entry, which was ignored. Nothing here makes a network request.
"""
import json

import pytest

from harness import desktop

BOM = b"\xef\xbb\xbf"
OFF = {"widgets": {"clock": {"weather": False}, "music": {"on": False}}}


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "desktop.json"
    monkeypatch.setattr(desktop, "CONFIG_PATH", str(path))
    monkeypatch.setattr(desktop, "COLLIE_DIR", str(tmp_path))
    monkeypatch.setattr(desktop, "_seed_apps", lambda: [])
    return path


@pytest.mark.parametrize("raw, enabled, readable", [
    (None, True, True),                                                    # no file: defaults
    (b"", True, True),                                                     # empty: defaults
    (json.dumps(OFF).encode(), False, True),
    (BOM + json.dumps(OFF).encode(), False, True),                         # PowerShell 5.1 utf8
    (BOM + json.dumps({"widgets": {"clock": {"slot": "bl"}}}).encode(), True, True),
    (b'{"widgets": {"clock": {"weather": false},}}', False, False),        # trailing comma
    (b'{"widgets": {"clock": {"slot": "bl"},}}', False, False),            # unreadable: off
    (b"[]", False, False),
    (b'{"widgets": {"clock": false}}', False, False),
    (b'{"widgets": null}', False, False),
    (b"\xff\xfe{\x00}\x00", False, False),                                 # UTF-16: not our text
    (json.dumps({"clock": {"weather": False}}).encode(), False, True),     # the 0.30.0 wording
])
def test_the_page_and_the_server_agree_and_fail_closed(config, raw, enabled, readable):
    if raw is not None:
        config.write_bytes(raw)
    assert desktop.weather_enabled() is enabled
    cfg = desktop.load_config()
    assert (cfg["widgets"]["clock"].get("weather") is not False) is enabled
    assert ("config_error" not in cfg) is readable
    if not readable:
        assert "desktop.json" in cfg["config_error"]


def test_a_byte_order_mark_no_longer_hides_the_rest_of_the_file(config):
    config.write_bytes(BOM + json.dumps(OFF).encode())
    cfg = desktop.load_config()
    assert cfg["widgets"]["music"]["on"] is False
    assert cfg["widgets"]["clock"]["slot"] == "tr"                 # untouched defaults still fill in


def test_an_unreadable_file_is_not_replaced(config):
    raw = b'{"widgets": {"clock": {"weather": false},}}'
    config.write_bytes(raw)
    with pytest.raises(ValueError, match="desktop.json was not changed"):
        desktop.save_config(desktop.load_config())
    assert config.read_bytes() == raw


def test_saving_writes_plain_utf8_and_drops_the_error_marker(config):
    config.write_bytes(BOM + json.dumps(OFF).encode())
    cfg = desktop.load_config()
    cfg["widgets"]["clock"]["slot"] = "bl"
    cfg["config_error"] = "left over from an earlier read"
    desktop.save_config(cfg)
    raw = config.read_bytes()
    assert not raw.startswith(BOM)
    saved = json.loads(raw)
    assert "config_error" not in saved
    assert saved["widgets"]["clock"] == {"on": True, "slot": "bl", "weather": False}
