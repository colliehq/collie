"""Opening a saved thread must cost what the thread costs, not what every message costs.

A long conversation is rendered in one synchronous pass by `openSession`: each turn appends a
bubble, and each append used to ask the browser where the box now ends — `scrollDown()` reads
`scrollHeight`/`nearBottom()` immediately after the DOM write, which forces a layout. That is three
forced layouts per turn (user bubble, assistant bubble, rendered markdown), so reopening a thread
did layout work proportional to its whole history before a single pixel of it could be seen.
`history_render_probe.py` measured exactly that against this page: 308 / 1508 / 3008 layouts for
100 / 500 / 1000 saved turns.

None of those intermediate positions is observable. The view is only ever seen once the pass ends,
so it only has to be measured once, at the end — which is what `hydrate()` scopes.

These tests drive the real harness/webui/index.html in Chromium against the same staged HTTP
fixture as test_web_ui_run_status, and read Chromium's own `Performance.getMetrics` `LayoutCount`
— the engine's count of layouts it actually performed, not a count of scrollDown() calls the page
could report about itself. What they assert is a *work* budget with generous headroom, never a wall
clock: a bulk history open may not scale its layout work with the number of messages.

Limits. Synthetic transcripts over intercepted HTTP in headless Chromium, one sample per
assertion; a machine under load changes the wall time of these tests but not the layout counts,
which is why no timing is asserted. Live output is the opposite case — every arrival IS visible —
so the last two tests hold the live mirror path, the follow behaviour and a reader's own scroll
position to exactly what they were.
"""
import json
import threading

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import ui, server, browser, _Fixture, RUNS   # noqa: F401

# Chromium lays out the shell, the sidebar rows, the composer and the scroll container around any
# thread open, and does some of it asynchronously. The point of the budget is the SHAPE of the
# cost, so it is set far above that fixed overhead (observed: 8) and far below a per-message cost
# (observed before the fix: 3 × turns + 8).
LAYOUT_BUDGET = 60

FIRST_ASK = "Why does the config loader skip the first data row?"
CONTEXT_BODY = "def load(path):\n    rows = read_csv(path)\n    return rows[1:]   # drops the header\n"
LAST_MARK = "Final saved answer: the loader now starts at row 1."
# 1x1 PNG — a pasted screenshot, kept as the saved message stored it.
PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
       "ScLbtAAAAABJRU5ErkJggg==")


def answer(index, last):
    """A saved answer in the shape people actually have: heading, prose, code, list, links."""
    return ("## Answer %d\n\n"
            "The loader skips row %d because the **header** is counted twice; see `loader.py`.\n\n"
            "```python\nrows = read_csv(path)[1:]   # turn %d\n```\n\n"
            "- first consequence of turn %d\n"
            "- second consequence of turn %d\n\n"
            "More detail in [the notes](https://example.invalid/%d).\n\n%s"
            % (index, index, index, index, index, index, LAST_MARK if last else "Continues below."))


def transcript(turns, receipts=None):
    """A saved thread of `turns` user+assistant pairs, with the awkward messages mixed IN.

    Turn 0 carries a display override and an attached file excerpt, turn 1 a pasted image, turn 2
    is steering. They are at the front precisely so that hundreds of ordinary messages are appended
    after them: whatever batches the bulk render must not lose the rare message inside it.
    """
    messages = []
    for index in range(turns):
        if index == 0:
            user = {"role": "user", "content": "RAW TEXT THE PAGE MUST NOT SHOW",
                    "display": {"text": FIRST_ASK,
                                "contexts": [{"label": "loader.py", "path": "harness/loader.py",
                                              "startLine": 10, "endLine": 24,
                                              "content": CONTEXT_BODY}]}}
        elif index == 1:
            user = {"role": "user", "content": [
                {"type": "text", "text": "Here is the screenshot of the failing import."},
                {"type": "image", "media_type": "image/png", "data": PNG}]}
        elif index == 2:
            user = {"role": "user", "kind": "steer",
                    "content": "Steering: check the header row as well."}
        else:
            user = {"role": "user", "content": "Question %d: why was row %d dropped?" % (index, index)}
        messages.append(user)
        messages.append({"role": "assistant", "content": answer(index, index == turns - 1)})
    return {"messages": messages, "run_receipts": receipts or []}


def stage(page, session, body):
    page.route("**/api/session/" + session,
               lambda route: route.fulfill(content_type="application/json",
                                           body=json.dumps(body, ensure_ascii=False)))


def layouts(page):
    """Chromium's own count of layouts performed, read over CDP."""
    cdp = page.context.new_cdp_session(page)
    cdp.send("Performance.enable")

    def read():
        return {m["name"]: m["value"] for m in cdp.send("Performance.getMetrics")["metrics"]}["LayoutCount"]
    return read


def box(page):
    return page.evaluate("""() => { const s = document.getElementById('scroll');
        return {top: s.scrollTop, height: s.scrollHeight, view: s.clientHeight}; }""")


def at_bottom(page):
    b = box(page)
    return b["height"] - b["top"] - b["view"] < 4


def open_thread(page, label, marker, timeout=30000):
    page.locator(".thread").filter(has_text=label).first.click(timeout=timeout)
    expect(page.locator("#log")).to_contain_text(marker, timeout=timeout)


def test_opening_a_long_saved_thread_renders_all_of_it_within_a_layout_budget(ui):
    """300 turns, in full, for a bounded amount of layout work — and the draft survives it."""
    turns, draft = 300, "Unsent draft that must survive opening a long thread"
    stage(ui.page, "s-read", transcript(turns))
    page = ui.page
    page.fill("#input", draft)

    count = layouts(page)
    before = count()
    open_thread(page, "Read README.md", LAST_MARK)
    used = count() - before

    assert used <= LAYOUT_BUDGET, (
        "opening %d saved turns performed %d layouts; the cost of a bulk history open must not "
        "scale with the number of messages in it" % (turns, used))

    # All of it arrived: every turn, and both ends whole.
    assert page.locator("#log .msg").count() == turns * 2
    assert page.locator("#log .msg.user").count() == turns
    # the first turn's own words — its own text node, since the attached-context panel is a
    # sibling inside the same element
    first = page.locator("#log .msg.user .bubble > .text").first
    assert first.evaluate("el => el.childNodes[0].textContent") == FIRST_ASK
    assert "RAW TEXT THE PAGE MUST NOT SHOW" not in ui.log_text()
    last = page.locator("#log .msg.assistant .text").last
    text = last.inner_text()
    assert "Answer %d" % (turns - 1) in text
    assert "rows = read_csv(path)[1:]   # turn %d" % (turns - 1) in text
    assert "second consequence of turn %d" % (turns - 1) in text
    assert text.rstrip().endswith(LAST_MARK), "the newest answer must be rendered to its last line"

    # Markdown was really rendered for every answer, not left as escaped source.
    assert page.locator("#log .msg.assistant .text.md").count() == turns
    assert page.locator("#log .msg.assistant .text.md pre code").count() == turns
    assert page.locator("#log .msg.assistant .text.md strong").count() == turns
    assert page.locator("#log .msg.assistant .text.md li").count() == turns * 2
    assert page.locator('#log .msg.assistant .text.md a[href="https://example.invalid/7"]').count() == 1

    # The one position that matters is the one a person sees: the end of the thread.
    assert at_bottom(page), "a reopened thread opens at its newest message"
    assert not page.locator("#scrollDownBtn").evaluate("el => el.classList.contains('show')")

    # A draft belongs to its thread. Typed here, it has to be here again after that whole history
    # has been rendered a second time on the way back — and nothing may have been sent meanwhile.
    page.fill("#input", draft)
    page.locator(".thread").filter(has_text="Migrate every module").first.click()
    page.wait_for_function("() => document.getElementById('input').value === ''", timeout=15000)
    open_thread(page, "Read README.md", LAST_MARK)
    assert page.input_value("#input") == draft
    assert page.locator("#log .msg").count() == turns * 2
    assert at_bottom(page)
    assert _Fixture.stream_requests == [], "opening history must never start a run"


def test_history_layout_work_does_not_grow_with_the_number_of_messages(ui):
    """The shape of the cost, stated directly: 16x the messages, no more layout work."""
    page = ui.page
    stage(page, "s-cap", transcript(25))
    stage(page, "s-read", transcript(400))
    count = layouts(page)

    before = count()
    open_thread(page, "Migrate every module", LAST_MARK)
    small = count() - before
    assert page.locator("#log .msg").count() == 50

    before = count()
    open_thread(page, "Read README.md", LAST_MARK)
    large = count() - before
    assert page.locator("#log .msg").count() == 800

    assert large <= small + 8, (
        "25 turns cost %d layouts and 400 turns cost %d: the difference means the page is still "
        "measuring itself per message" % (small, large))
    assert small <= LAYOUT_BUDGET and large <= LAYOUT_BUDGET


def test_a_bulk_rendered_thread_keeps_context_images_display_text_and_its_outcome(ui):
    """Batching the render may not cost a single stored detail of the messages inside it."""
    page = ui.page
    stopped = [{"run": "r-stop", "stop_reason": "canceled", "completed": False, "turns": 3,
                "edited": False, "verified": False, "canceled": True, "error": "",
                "decision": {"intent": "build", "verification": "auto"}}]
    stage(page, "s-read", transcript(150, stopped))
    open_thread(page, "Read README.md", LAST_MARK)

    # the attached file excerpt, whole
    panel = page.locator("#log .msg.user .attached-context").first
    assert panel.locator("summary").text_content() == "loader.py · 10–24"
    assert panel.locator("pre").text_content() == CONTEXT_BODY
    assert page.locator("#log .attached-context").count() == 1

    # the pasted screenshot, as stored
    image = page.locator("#log .msg.user .imgs img").first
    assert image.get_attribute("src") == "data:image/png;base64," + PNG
    assert image.evaluate("el => el.complete && el.naturalWidth > 0"), "the image really decoded"
    assert page.locator("#log .msg.user .imgs img").count() == 1
    assert "Here is the screenshot of the failing import." in ui.log_text()

    # steering is still marked as steering, 147 messages further up the thread
    steer = page.locator("#log .msg.user.steer")
    assert steer.count() == 1
    assert "steering" in steer.locator(".who").text_content()

    # and the thread still says how its last run ended
    assert "Run stopped by the user." in page.locator(".interruption-note").inner_text()
    assert page.locator("#log .msg").count() == 300
    assert _Fixture.stream_requests == []


def test_a_message_that_fails_to_render_leaves_the_page_able_to_scroll(ui):
    """Batching keeps state, so the failing path has to put it back — every time.

    A malformed saved message (`contexts` that is not a list) throws in the middle of the bulk
    render. If that left the batching flag set, the page would be silently unable to scroll itself
    for the rest of its life — including for the next, perfectly good thread.
    """
    page = ui.page
    broken = transcript(30)
    broken["messages"][0]["display"]["contexts"] = "not a list"
    stage(page, "s-cap", broken)
    stage(page, "s-read", transcript(120))

    page.locator(".thread").filter(has_text="Migrate every module").first.click()
    expect(page.locator("#log .err")).to_contain_text("failed to load", timeout=15000)

    open_thread(page, "Read README.md", LAST_MARK)
    assert page.locator("#log .msg").count() == 240
    assert at_bottom(page), "the next thread still opens at its newest message"


def test_a_late_transcript_cannot_overwrite_the_long_thread_that_replaced_it(ui):
    """Session ownership and the late-response guard survive the batched render."""
    page = ui.page
    page.evaluate("""() => {
      const original = window.fetch.bind(window);
      window.fetch = (url, options) => String(url) === '/api/session/s-cap'
        ? new Promise(resolve => { window.releaseOldThread = () => resolve(new Response(
            JSON.stringify({messages:[{role:'user',content:'OLD THREAD MUST NOT APPEAR'}]}),
            {headers:{'content-type':'application/json'}})); })
        : original(url, options);
    }""")
    stage(page, "s-read", transcript(200))
    page.locator(".thread").filter(has_text="Migrate every module").click()
    page.wait_for_function("() => typeof window.releaseOldThread === 'function'")
    open_thread(page, "Read README.md", LAST_MARK)

    page.evaluate("() => window.releaseOldThread()")
    page.wait_for_timeout(200)
    assert "OLD THREAD MUST NOT APPEAR" not in ui.log_text()
    assert page.locator("#log .msg").count() == 400
    assert ui.title() == "Read README.md and tell me what this tool does"


def test_live_output_still_follows_and_never_yanks_back_a_reader_who_scrolled_up(ui):
    """The control: live arrivals ARE visible, so they keep measuring — and keep their manners.

    A run this window did not start is followed over the real mirror stream, so each step lands on
    its own, the way streamed output does. Following must still work after a thread was bulk
    rendered, and a person who scrolls up to read must keep their place while steps keep arriving.
    """
    page = ui.page
    state = {"calls": 0, "phase": "first", "sent": set()}
    lock = threading.Lock()

    def runs(route):
        route.fulfill(json={"runs": [dict(RUNS[0], state="running", stop_reason="",
                                          can_steer=True)]})

    def mirror(route):
        with lock:
            state["calls"] += 1
            phase = state["phase"] if state["phase"] not in state["sent"] else None
            if phase:
                state["sent"].add(phase)
        # No `done`: the stream just ends, and the page's EventSource reconnects (retry below),
        # which is how a second batch can be delivered at a moment this test chooses.
        steps = "".join(
            "event: tool\ndata: %s\n\n" % json.dumps(
                {"name": "read_file", "args": {"path": "module_%s_%02d.py" % (phase, i)}})
            for i in range(12)) if phase else ": waiting\n\n"
        route.fulfill(content_type="text/event-stream", body="retry: 120\n\n" + steps)

    stage(page, "s-cap", transcript(40))
    page.route("**/api/runs", runs)
    page.route("**/api/mirror?*", mirror)
    page.reload(wait_until="load")
    page.wait_for_selector("#input", timeout=8000)
    open_thread(page, "Migrate every module", LAST_MARK)

    # live follow: steps arriving one at a time keep the view on the newest one
    expect(page.locator("#log")).to_contain_text("module_first_11.py", timeout=15000)
    assert at_bottom(page), "live output must still follow the end of the thread"

    # a real wheel gesture upward: this reader is now reading, not watching
    page.mouse.move(640, 400)
    page.mouse.wheel(0, -6000)
    page.wait_for_function("() => document.getElementById('scrollDownBtn').classList.contains('show')",
                           timeout=8000)
    parked = box(page)["top"]
    assert parked > 0 and not at_bottom(page)

    with lock:
        state["phase"] = "second"
    expect(page.locator("#log")).to_contain_text("module_second_11.py", timeout=15000)
    assert box(page)["top"] == parked, "arriving output must not yank a reader back to the bottom"
    assert page.locator("#scrollDownBtn").evaluate("el => el.classList.contains('show')")

    # Jump-to-latest still ends the standoff, and following resumes from there
    page.locator("#scrollDownBtn").click()
    page.wait_for_function("""() => { const s = document.getElementById('scroll');
        return s.scrollHeight - s.scrollTop - s.clientHeight < 4; }""", timeout=8000)
    assert not page.locator("#scrollDownBtn").evaluate("el => el.classList.contains('show')")
    assert state["calls"] >= 2, "the mirror stream was really opened and reopened"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
