import time

from harness.ambient import AmbientObserver
from harness.procedure_memory import ProcedureMemory


class _Source:
    def __init__(self):
        self.apps = ["Code.exe", "chrome.exe"]

    def foreground_app(self):
        return self.apps.pop(0)

    def idle_seconds(self):
        return 0

    def system_signals(self, **_):
        return []


def test_ambient_observer_is_content_free_and_silent_until_opted_in(tmp_path):
    path = str(tmp_path / "procedures.db")
    now = time.time()
    observer = AmbientObserver(path, source=_Source())
    try:
        assert observer.tick(now=now)["enabled"] is False
        with ProcedureMemory(path) as settings:
            settings.update_privacy(observation_mode="activity", consent=True,
                                    ambient_sample_seconds=2)
        observer.tick(now=now + 1)
        observer.tick(now=now + 11)
        rows = observer.store.list_events()
        assert len(rows) == 1
        assert rows[0]["project"] == "@ambient"
        assert rows[0]["app"] == "code"
        assert rows[0]["object_ref"] == ""
        assert rows[0]["metadata"] == {"ambient": True, "duration_s": 15}
    finally:
        observer.close()
