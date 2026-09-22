def test_stdio_child_env_keeps_windows_runtime_but_not_parent_secrets(monkeypatch):
    from harness import mcpclient

    parent = {
        "PATH": "C:/Windows/System32",
        "SystemRoot": "C:/Windows",
        "WINDIR": "C:/Windows",
        "TEMP": "C:/Temp",
        "TMP": "C:/Temp",
        "USERPROFILE": "C:/Users/tester",
        "APPDATA": "C:/Users/tester/AppData/Roaming",
        "LOCALAPPDATA": "C:/Users/tester/AppData/Local",
        "OPENAI_API_KEY": "must-not-leak",
        "ANTHROPIC_API_KEY": "must-not-leak",
        "GITHUB_TOKEN": "must-not-leak",
    }
    monkeypatch.setattr(mcpclient.os, "environ", parent)

    child = mcpclient._child_env({"env": {"COMFY_NO_TELEMETRY": 1}})

    for name in ("SystemRoot", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA",
                 "LOCALAPPDATA"):
        assert child[name] == parent[name]
    assert child["COMFY_NO_TELEMETRY"] == "1"
    assert not ({"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN"} & child.keys())
