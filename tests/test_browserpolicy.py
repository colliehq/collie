"""Which sites the browser may open without asking again, at the policy's own boundary.

The gate calls navigation_allowed_without_prompt(); until now it was reached only through the
gate, so a sensitive-site rule that stopped matching would have shown up as a bank opening with
no confirmation rather than as a failing test.
"""
import pytest

from harness import browserpolicy as bp


@pytest.mark.parametrize("url", [
    "https://www.chase.com/login",
    "https://CHASE.COM./",                    # case and a trailing dot
    "https://secure.paypal.com/",
    "https://mybank.bank/",
    "https://online-banking.example/",
    "https://wallet.example.org/",
    "https://payments.example.net/",
    "not a url",                              # unparseable: keep asking
    "",
])
def test_sensitive_sites_keep_their_confirmation(url):
    assert bp.is_sensitive_site(url)
    assert not bp.navigation_allowed_without_prompt("all_except_sensitive", url)


@pytest.mark.parametrize("url", [
    "https://github.com/colliehq/collie",
    "https://notchase.com/",                  # a suffix is a whole label, not a substring
    "https://bankrupt-news.example/",         # "bank" inside a longer word
    "https://evil.test/chase.com",            # the path is not the host
    "https://chase.com@evil.test/",           # userinfo is not the host either
    "http://localhost:8787/",
    "http://127.0.0.1:8787/",
])
def test_ordinary_sites_do_not(url):
    assert not bp.is_sensitive_site(url)


def test_extra_patterns_cover_subdomains_and_wildcards():
    extra = "corp.example, *.finance.test"
    assert bp.is_sensitive_site("https://hr.corp.example/", extra)
    assert bp.is_sensitive_site("https://corp.example/", extra)
    assert bp.is_sensitive_site("https://pay.finance.test/", extra)
    assert not bp.is_sensitive_site("https://notcorp.example/", extra)
    assert bp.is_sensitive_site("https://hr.corp.example/", ["corp.example"])   # list form too


@pytest.mark.parametrize("policy, url, allowed", [
    ("ask_every_site", "https://github.com/", False),
    ("all_sites", "https://github.com/", True),
    ("all_sites", "https://www.chase.com/", True),         # the user chose every site
    ("all_sites", "not a url", False),
    ("all_except_sensitive", "https://github.com/", True),
    ("all_except_sensitive", "https://www.chase.com/", False),
    ("something_new", "https://github.com/", False),       # an unknown policy never widens
    ("", "https://github.com/", False),
])
def test_policies(policy, url, allowed):
    assert bp.navigation_allowed_without_prompt(policy, url) is allowed
