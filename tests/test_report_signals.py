"""The one shape every morning-report source speaks, and the runner that reads them.

A signal is a normalized fact about the person's day, attached to a project.  These tests pin the
schema (stable ids, bounded text, https-only links, known kinds) and the runner's promises: every
source is read in parallel inside its own time limit, a source that fails costs only itself and
says why, and a source can never flood the report.  No network, no real state.
"""
import threading
import time

import pytest

from harness import report_signals as rs


NOW = 1790000000.0


def ctx(**kw):
    return rs.Context(now=NOW, state_dir=kw.pop("state_dir", ""), **kw)


# ---------------------------------------------------------------- the schema


def test_a_signal_has_exactly_the_schema_fields_and_a_stable_id():
    one = rs.make("github", "pr:colliehq/collie#28", kind="done", title="PR merged",
                  project="colliehq/collie", detail="#28", when=NOW, evidence="gh graphql")
    again = rs.make("github", "pr:colliehq/collie#28", kind="done", title="changed title",
                    project="colliehq/collie")
    other = rs.make("gmail", "pr:colliehq/collie#28", kind="done", title="PR merged",
                    project="colliehq/collie")
    assert set(one) == {"id", "source", "project", "kind", "title", "detail", "when", "link",
                        "evidence", "untrusted"}
    assert one["id"] == again["id"]            # the id names the thing, not its wording
    assert one["id"] != other["id"]            # ...and which source said it
    assert one["id"].startswith("github-")
    assert one["when"] == NOW and one["untrusted"] is False


def test_unknown_kinds_are_refused():
    with pytest.raises(ValueError):
        rs.make("github", "x", kind="urgent", title="t", project="p")


def test_text_is_one_bounded_line_without_control_characters():
    sig = rs.make("gmail", "m1", kind="fyi", title="Hi\x00\x1b[31m there\n\nfriend" + "x" * 900,
                  detail="a\tb\r\nc" + "y" * 900, project="", untrusted=True)
    assert "\n" not in sig["title"] and "\x1b" not in sig["title"] and "\x00" not in sig["title"]
    assert len(sig["title"]) <= rs.TITLE_LIMIT and len(sig["detail"]) <= rs.DETAIL_LIMIT
    assert sig["project"] == "source:gmail"     # no project -> the source's own bucket
    assert sig["untrusted"] is True


@pytest.mark.parametrize("link, kept", [
    ("https://github.com/colliehq/collie/pull/28", True),
    ("http://github.com/colliehq/collie", False),
    ("javascript:alert(1)", False),
    ("/brief?todo=1", False),                  # a local route is not a link in an email
    ("https://user:pw@example.com/", False),
    ("https://exa mple.com/", False),
    ("//evil.example/x", False),
])
def test_links_are_https_only(link, kept):
    sig = rs.make("news", "n", kind="fyi", title="t", project="", link=link)
    assert (sig["link"] == link) is kept
    if not kept:
        assert sig["link"] == ""


def test_timestamps_that_are_not_real_instants_become_none():
    for bad in (0, -5, "soon", float("nan"), 9e12, None):
        assert rs.make("collie", "x", kind="fyi", title="t", project="", when=bad)["when"] is None


def test_meta_is_private_adapter_data_and_never_part_of_the_public_signal():
    sig = rs.make("gmail", "t1", kind="needs_you", title="Invoice", project="",
                  meta={"thread_id": "t1", "from": "Billing <billing@example.com>"})
    assert sig["meta"]["thread_id"] == "t1"
    public = rs.public(sig)
    assert "meta" not in public and set(public) == set(rs.FIELDS)


@pytest.mark.parametrize("url, slug", [
    ("https://github.com/colliehq/collie.git", "colliehq/collie"),
    ("https://github.com/colliehq/collie", "colliehq/collie"),
    ("git@github.com:wudaming00/fluent-me.git", "wudaming00/fluent-me"),
    ("ssh://git@github.com/Comfy-Org/ComfyUI_frontend.git", "Comfy-Org/ComfyUI_frontend"),
    ("https://token@github.com/a/b.git", "a/b"),
    ("https://gitlab.com/a/b.git", ""),
    ("C:/repos/local.git", ""),
    ("", ""),
])
def test_github_slug_from_a_remote_url(url, slug):
    assert rs.github_slug(url) == slug


def test_projects_merge_case_insensitively_and_source_buckets_are_not_projects():
    signals = [rs.make("github", "a", kind="done", title="t", project="Comfy-Org/ComfyUI_frontend"),
               rs.make("local", "b", kind="stale", title="t", project="comfy-org/comfyui_frontend"),
               rs.make("gmail", "c", kind="fyi", title="t", project="")]
    groups = rs.group_by_project(signals)
    assert list(groups) == ["Comfy-Org/ComfyUI_frontend"]
    assert len(groups["Comfy-Org/ComfyUI_frontend"]) == 2
    assert rs.is_project("colliehq/collie") and not rs.is_project("source:gmail")


# ---------------------------------------------------------------- the runner


def _adapter(name, fn, timeout=5.0):
    return rs.Adapter(name=name, label=name.title(), read=fn, timeout=timeout)


def test_every_source_is_read_and_reported_with_its_own_state():
    def good(c):
        return {"signals": [rs.make("github", "1", kind="done", title="shipped", project="a/b")],
                "counters": {"gh.stars:a/b": 3}, "detail": "2 calls"}

    def off(c):
        raise rs.Unavailable("Google isn't connected yet")

    def broken(c):
        raise RuntimeError("C:\\Users\\someone\\secret path and a mail body")

    signals, sources, counters = rs.collect(ctx(), [_adapter("github", good),
                                                    _adapter("gmail", off),
                                                    _adapter("calendar", broken)])
    rows = {row["name"]: row for row in sources}
    assert rows["github"]["state"] == "ok" and rows["github"]["signals"] == 1
    assert rows["github"]["detail"] == "2 calls"
    assert rows["gmail"]["state"] == "unavailable"
    assert rows["gmail"]["reason"] == "Google isn't connected yet"
    assert rows["calendar"]["state"] == "unavailable"
    # An exception's text can hold paths and private rows; only its type is reported.
    assert "secret" not in rows["calendar"]["reason"] and "RuntimeError" in rows["calendar"]["reason"]
    assert all(row["read_at"] and row["took_ms"] >= 0 for row in sources)
    assert [s["title"] for s in signals] == ["shipped"] and counters == {"gh.stars:a/b": 3}


def test_sources_run_in_parallel_and_a_slow_one_is_cut_off():
    started = threading.Event()

    def slow(c):
        started.set()
        time.sleep(3)
        return {"signals": [rs.make("local", "x", kind="stale", title="late", project="p")]}

    def quick(c):
        assert started.wait(2), "the quick source waited for the slow one"
        return {"signals": []}

    t0 = time.monotonic()
    signals, sources, _ = rs.collect(ctx(), [_adapter("local", slow, timeout=0.3),
                                             _adapter("news", quick)])
    assert time.monotonic() - t0 < 2.5
    rows = {row["name"]: row for row in sources}
    assert rows["local"]["state"] == "unavailable" and "took longer" in rows["local"]["reason"]
    assert rows["news"]["state"] == "ok"
    assert signals == []                        # a late answer never sneaks in


def test_a_source_cannot_flood_the_report():
    def flood(c):
        return {"signals": [rs.make("news", str(i), kind="fyi", title="h%d" % i, project="")
                            for i in range(rs.MAX_SIGNALS_PER_SOURCE + 25)]}

    signals, sources, _ = rs.collect(ctx(), [_adapter("news", flood)])
    assert len(signals) == rs.MAX_SIGNALS_PER_SOURCE
    assert sources[0]["state"] == "partial" and "25" in sources[0]["reason"]


def test_malformed_and_duplicate_signals_are_dropped_and_the_source_marked_partial():
    good = rs.make("collie", "1", kind="done", title="ok", project="")

    def mixed(c):
        return {"signals": [good, dict(good), {"id": "x", "kind": "urgent"}, "junk"]}

    signals, sources, _ = rs.collect(ctx(), [_adapter("collie", mixed)])
    assert [s["id"] for s in signals] == [good["id"]]
    assert sources[0]["state"] == "partial"


def test_an_adapter_may_report_partial_itself():
    def partial(c):
        return {"state": "partial", "reason": "2 repos timed out", "signals": []}

    _, sources, _ = rs.collect(ctx(), [_adapter("local", partial)])
    assert sources[0]["state"] == "partial" and sources[0]["reason"] == "2 repos timed out"


def test_a_source_that_returns_nonsense_is_unavailable_not_empty():
    _, sources, _ = rs.collect(ctx(), [_adapter("collie", lambda c: None)])
    assert sources[0]["state"] == "unavailable"


def test_adapters_are_a_registry_so_a_new_source_changes_no_report_code():
    seen = []

    def extra(c):
        seen.append(c.now)
        return {"signals": [rs.make("stripe", "p1", kind="done", title="New payment", project="")]}

    signals, sources, _ = rs.collect(ctx(), [_adapter("stripe", extra)])
    assert seen == [NOW] and signals[0]["source"] == "stripe"
    assert sources[0]["label"] == "Stripe"


# ---------------------------------------------------------------- projects: activity and aliases


def test_sources_report_when_the_person_last_worked_on_a_project():
    def github(c):
        return {"signals": [rs.make("github", "r", kind="done", title="v1 is out", project="Owner/Repo")],
                "activity": {"Owner/Repo": NOW - 3600, "source:github": NOW, "bad": "yesterday"}}

    def local(c):
        return {"signals": [], "activity": {"owner/repo": NOW - 60, "Other": NOW - 7200}}

    got = rs.collect(ctx(), [_adapter("github", github), _adapter("local", local)])
    signals, sources, counters = got                  # still unpacks as before
    assert got.activity == {"Owner/Repo": NOW - 60, "Other": NOW - 7200}


def test_one_checkout_with_two_remotes_makes_them_one_project():
    def local(c):
        return {"signals": [rs.make("local", "x", kind="fyi", title="2 commits not pushed yet on main",
                                    project="wudaming00/collie")],
                "activity": {"wudaming00/collie": NOW - 60},
                "aliases": [["wudaming00/collie", "colliehq/collie"]]}

    def github(c):
        return {"signals": [rs.make("github", n, kind="done", title=n, project="colliehq/collie")
                            for n in ("release", "merged")],
                "activity": {"colliehq/collie": NOW - 86400}}

    got = rs.collect(ctx(), [_adapter("local", local), _adapter("github", github)])
    assert {s["project"] for s in got[0]} == {"colliehq/collie"}   # the name most signals use
    assert got.activity == {"colliehq/collie": NOW - 60}
    assert list(rs.group_by_project(got[0])) == ["colliehq/collie"]
