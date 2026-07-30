"""desktop.play_here — music that comes out of the computer you asked.

collie could already FIND music: yt-dlp resolves a stream in about a second. What it never did was
play it — `resolve_audio` handed the URL to whichever screen asked, so a client with no audio element
(a phone, `collie web` in a terminal) got a correct answer and silence.

Nothing here touches the network or makes a sound; the resolver and the player are both stubbed, and
what is checked is the decision-making around them.

    python3 tests/test_playhere.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import desktop as dt                                    # noqa: E402
from harness import plat                                             # noqa: E402

_fails = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


class FakeProc(object):
    def __init__(self):
        self.killed = False
        self._done = None
        # play_here records the player's pid so a later collie (or a reaper after a crash) can stop
        # music this process did not start. A stand-in without one turns that into an AttributeError
        # from inside the code under test, which reads as the feature being broken rather than the
        # double being out of date.
        self.pid = 424242

    def poll(self):
        return self._done


def main():
    started, stopped = [], []

    def fake_resolve(query, artist="", title="", region="", exclude=()):
        if "nothing" in query:
            return {"ok": False, "error": "no results"}
        return {"ok": True, "url": "https://stream/" + query, "title": "Track: " + query,
                "uploader": "Someone", "duration": 123}

    def fake_play(url):
        p = FakeProc()
        started.append(url)
        return p

    def fake_stop(proc):
        if proc is None:
            return False
        proc.killed = True
        stopped.append(proc)
        return True

    real = (dt.resolve_audio, plat.play_stream, plat.stop_stream)
    dt.resolve_audio, plat.play_stream, plat.stop_stream = fake_resolve, fake_play, fake_stop
    try:
        r = dt.play_here("Cruel Summer", title="Cruel Summer")
        check(r.get("ok") is True, "a resolvable track plays")
        check(started == ["https://stream/Cruel Summer"], "the resolved URL is what gets played")
        check(r.get("title") == "Track: Cruel Summer", "the reply names the track that was found")
        check(r.get("stoppable") is True, "a headless player reports that it can be stopped")
        check(dt.playing_here()["track"]["title"] == "Track: Cruel Summer",
              "and it is remembered as now-playing")

        # A second request replaces the first: two songs at once is never what was meant.
        first = dt._playing["proc"]
        dt.play_here("Blank Space")
        check(first.killed, "asking for another track stops the one that was playing")
        check(len(started) == 2, "and starts the new one")

        r2 = dt.stop_here()
        check(r2.get("ok") is True, "stopping works")
        check(dt.playing_here()["track"] is None, "and clears now-playing")
        check(dt.stop_here().get("ok") is False, "stopping again is a quiet false, not an error")

        # Nothing found must not report a success — the whole point of the original bug.
        r3 = dt.play_here("nothing at all")
        check(r3.get("ok") is False and r3.get("error"), "an unresolvable track fails honestly")
        check(dt.playing_here()["track"] is None, "and does not leave a phantom now-playing")

        # A player the platform opened for us (QuickTime, a browser) is not ours to kill.
        plat.play_stream = lambda url: None
        r4 = dt.play_here("Style")
        check(r4.get("ok") is True and r4.get("stoppable") is False,
              "when the platform's own player takes it, the reply says stopping won't work")

        # A track that ended on its own should stop being reported as playing.
        plat.play_stream = fake_play
        dt.play_here("Delicate")
        dt._playing["proc"]._done = 0
        check(dt.playing_here()["track"] is None, "a finished track is no longer now-playing")
    finally:
        dt.resolve_audio, plat.play_stream, plat.stop_stream = real
        dt._playing["proc"], dt._playing["track"] = None, None

    # "stop the music" has to stop it. The router only ever ANSWERED action=stop and left the caller
    # to pause its own player — which was fine when the caller had one. Now the desktop is the player,
    # and for a while nothing on this machine could stop it: no button, and the words did nothing.
    from harness import webapp

    plat.play_stream, plat.stop_stream = fake_play, fake_stop
    dt.resolve_audio = fake_resolve
    try:
        dt.play_here("Cruel Summer")
        proc = dt._playing["proc"]
        r = {"action": "stop", "arg": ""}
        if dt.playing_here().get("track"):
            dt.stop_here()
            r["stopped_audio"] = True
        check(proc.killed, "a stop request kills what the desktop was playing")
        check(webapp._intent_summary(r) == "Stopped the music.",
              "and says what it stopped, not a bare 'Stopped'")
        check(webapp._intent_summary({"action": "stop", "arg": ""}) == "Stopped.",
              "while a stop with nothing playing stays generic")
    finally:
        dt.resolve_audio, plat.play_stream, plat.stop_stream = real
        dt._playing["proc"], dt._playing["track"] = None, None

    print("\n  " + ("%d FAILED" % len(_fails) if _fails else "play here: all green"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
