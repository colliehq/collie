"""The page the loopback handler shows after Google hands the sign-in back."""
import re

import pytest

from harness import google_connect as gc

READ, COMPOSE, CAL = gc.GMAIL_READ, gc.GMAIL_COMPOSE, gc.CALENDAR_READ


def _text(page):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))


def _self_contained(page):
    assert page.lstrip().lower().startswith("<!doctype html>")
    assert "<script" not in page.lower()
    assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", page, re.I)
    assert "@import" not in page and "url(http" not in page and "@font-face" not in page
    assert "prefers-color-scheme: dark" in page       # light and dark


def test_success_page_names_the_account_and_every_granted_permission():
    page = gc.success_page([READ, COMPOSE, CAL], account="owner@example.com", dog_name="")
    _self_contained(page)
    text = _text(page)
    assert "You're connected!" in text or "You&#x27;re connected!" in text
    assert "owner@example.com" in text
    assert "read your mail and write drafts" in text and "Collie never sends" in text
    assert "read your events" in text
    assert "not allowed" not in text.lower()
    assert "tomorrow morning" in text and "by email and on your desktop" in text
    assert "stored only on this computer" in text
    assert "collie google disconnect" in text and "Settings" in text
    assert "You can close this tab" in text
    assert "#58aaff" in page and "#ffe2ad" in page and "#1fa971" in page
    assert "ui-rounded" in page


def test_success_page_explains_each_scope_that_was_not_granted():
    page = gc.success_page([READ], account="owner@example.com")
    text = _text(page)
    assert "read your mail" in text
    assert text.lower().count("not allowed") == 2
    assert "drafts" in text and "calendar" in text.lower()
    assert "collie google connect" in text        # how to fix it


def test_success_page_with_only_drafts_says_mail_cannot_be_read():
    text = _text(gc.success_page([COMPOSE, CAL], account="a@b.c"))
    assert "write drafts" in text and "Collie never sends" in text
    assert "not allowed" in text.lower() and "read" in text.lower()


def test_success_page_escapes_everything_it_is_given():
    evil = '<script>alert("x")</script>@example.com'
    page = gc.success_page([READ, COMPOSE, CAL], account=evil, dog_name='<img src=x onerror=1>')
    assert "<script>alert" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page


def test_success_page_carries_the_dog_inline():
    page = gc.success_page([READ, COMPOSE, CAL], account="a@b.c")
    assert re.search(r'src="data:image/(svg\+xml|png);base64,[A-Za-z0-9+/=]{200,}"', page)
    assert len(page.encode("utf-8")) < 120_000


@pytest.mark.parametrize("reason,words", [
    ("denied", "nothing was connected"),
    ("state_mismatch", "didn't match"),
    ("no_scopes", "nothing was allowed"),
    ("error", "something went wrong"),
    ("timeout", "something went wrong"),
])
def test_failure_pages_say_what_happened_and_how_to_retry(reason, words):
    page = gc.failure_page(reason, detail='token endpoint said <b>"invalid_client"</b>')
    _self_contained(page)
    text = _text(page).lower()
    assert words in text
    assert "collie google connect" in text and "settings" in text
    assert "you can close this tab" in text
    assert "<b>\"invalid_client\"" not in page
