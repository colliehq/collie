"""Browser waits must pass a function, not a bare expression string.

Playwright answers `wait_for_function("<expression>")` directly when the first check is already
true, but once it has to poll it compiles the expression with eval. Collie's pages send a CSP
without 'unsafe-eval', so that poll fails with EvalError: the test passes on a fast machine and
fails on a slow runner (automation_editor_ui_check did, on Ubuntu). A string that is a function
("() => ...") is compiled by Playwright itself and polls fine under the same CSP.
"""
import pathlib
import re

TESTS = pathlib.Path(__file__).resolve().parent
CALL = re.compile(r"""wait_for_function\(\s*(?!'''|\"\"\")(?P<q>['"])(?P<body>(?:\\.|(?!(?P=q)).)*)(?P=q)""", re.S)
FUNCTION = re.compile(r"""^\s*(\(|async\b|function\b|[A-Za-z_$][\w$]*\s*=>)""")


def test_every_wait_for_function_string_is_a_function():
    bare = []
    for path in sorted(TESTS.glob("*.py")):
        if path.name == pathlib.Path(__file__).name:     # its own docstring shows the bad form
            continue
        text = path.read_text(encoding="utf-8")
        for match in CALL.finditer(text):
            if not FUNCTION.match(match.group("body")):
                line = text.count("\n", 0, match.start()) + 1
                bare.append("%s:%d %s" % (path.name, line, match.group("body")[:60]))
    assert not bare, ("wait_for_function got a bare expression; write it as \"() => ...\" so it "
                      "still polls under the page's CSP:\n" + "\n".join(bare))
