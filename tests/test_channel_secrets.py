from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from harness import channel_secrets as secrets


def test_parallel_connections_do_not_overwrite_each_other(tmp_path):
    def save(index):
        secrets.put("account-%d" % index, {"password": "private-%d" % index}, state_dir=str(tmp_path))
    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(save, range(12)))
    for index in range(12):
        assert secrets.get("account-%d" % index, state_dir=str(tmp_path)) == {"password": "private-%d" % index}
        assert secrets.present("account-%d" % index, state_dir=str(tmp_path)) == ["password"]
    secrets.delete("account-0", state_dir=str(tmp_path))
    assert not secrets.get("account-0", state_dir=str(tmp_path))
    assert secrets.get("account-1", state_dir=str(tmp_path))


def test_broken_credentials_are_preserved_for_repair(tmp_path):
    path = tmp_path / "channel-credentials.json"
    path.write_text('{"smtp":', encoding="utf-8")
    with pytest.raises(ValueError, match="needs repair"):
        secrets.put("twilio", {"auth_token": "new-secret"}, state_dir=str(tmp_path))
    assert path.read_text(encoding="utf-8") == '{"smtp":'


def test_secret_validation_never_echoes_the_value(tmp_path):
    for credentials in ({"secret-from-unknown-provider": "TOP-SECRET"}, {"password": "TOP-SECRET\x00"}):
        with pytest.raises(ValueError) as error:
            secrets.put("mail", credentials, state_dir=str(tmp_path))
        assert "TOP-SECRET" not in str(error.value)
    assert not (tmp_path / "channel-credentials.json").exists()
