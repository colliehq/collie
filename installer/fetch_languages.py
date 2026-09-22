"""Vendor unofficial Inno Setup translation sources and prove each can compile warning-clean.

Inno ships ~30 translations in its own Languages folder; another ~45 live in the upstream repo as
"unofficial" (Vietnamese, Indonesian, Greek, Hindi, Farsi, Romanian, Croatian... — none of them
available to a plain `iscc` install). We vendor those into installer/lang/ so a clone has stable
upstream sources without manual file copying.

Unofficial translations can lag the installed Inno release. ``gen_langs.py`` normalizes each source
against that compiler's current ``Default.isl``: valid translations survive, obsolete keys are
removed, and missing messages receive the official English default. This tool test-compiles those
normalized files and treats warnings as failures, rather than accepting Inno's successful exit code
while overlooking schema drift.

    python installer/fetch_languages.py          # download + verify + print the [Languages] block
    python installer/fetch_languages.py --check  # verify what's already vendored, download nothing
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

from gen_langs import normalize_vendor_language

HERE = os.path.dirname(os.path.abspath(__file__))
LANG = os.path.join(HERE, "lang")
API = "https://api.github.com/repos/jrsoftware/issrc/contents/Files/Languages/Unofficial"
# Traditional Chinese is in neither place upstream; WinMerge maintains an Inno 6 one.
EXTRA = {"ChineseTraditional.isl":
         "https://raw.githubusercontent.com/WinMerge/winmerge/master/Translations/InnoSetup/"
         "Unbundled.is6/ChineseTraditional.isl"}


def iscc():
    for p in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"),
              r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
              r"C:\Program Files\Inno Setup 6\ISCC.exe"):
        if os.path.isfile(p):
            return p
    sys.exit("ISCC.exe not found — install Inno Setup 6.")


def fetch(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "collie-installer-build"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if data.startswith(b"404"):
        return False
    with open(dest, "wb") as f:
        f.write(data)
    return True


def download():
    os.makedirs(LANG, exist_ok=True)
    req = urllib.request.Request(API, headers={"User-Agent": "collie-installer-build"})
    with urllib.request.urlopen(req, timeout=60) as r:
        entries = json.load(r)
    got = []
    for e in entries:
        if not e["name"].lower().endswith((".isl", ".islu")):
            continue
        dest = os.path.join(LANG, e["name"])
        if fetch(e["download_url"], dest):
            got.append(e["name"])
    for name, url in EXTRA.items():
        if fetch(url, os.path.join(LANG, name)):
            got.append(name)
    return sorted(got)


def language_name(path):
    """The native name, kept in the .isl's own <hex> escaping so we can re-emit it verbatim."""
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            txt = open(path, encoding=enc).read()
        except (UnicodeDecodeError, LookupError):
            continue
        m = re.search(r"^LanguageName=(.*)$", txt, re.M)
        if m:
            return m.group(1).strip()
    return ""


def compiles(compiler, msgfile):
    """Normalize and test-compile one language; success means zero compiler warnings."""
    with tempfile.TemporaryDirectory() as td:
        compatible = os.path.join(td, "compatible.islu")
        try:
            normalize_vendor_language(
                msgfile,
                os.path.join(os.path.dirname(compiler), "Default.isl"),
                compatible,
                bundled_dir=os.path.join(os.path.dirname(compiler), "Languages"),
            )
        except (OSError, UnicodeError, ValueError) as exc:
            return False, ["normalization failed: %s" % str(exc)]
        iss = os.path.join(td, "t.iss")
        with open(iss, "wb") as f:
            f.write(("[Setup]\nAppName=T\nAppVersion=1\nDefaultDirName={tmp}\\t\nOutputDir=%s\n"
                     "PrivilegesRequired=lowest\nUninstallable=no\nCreateAppDir=no\n\n"
                     "[Languages]\nName: \"x\"; MessagesFile: \"%s\"\n"
                     % (td, compatible.replace("\\", "\\\\"))).encode("utf-8"))
        r = subprocess.run([compiler, iss], capture_output=True, text=True)
        output = (r.stdout + r.stderr).strip().splitlines()
        warnings = [line for line in output if re.match(r"^\s*Warning:", line, re.I)]
        good = r.returncode == 0 and not warnings
        return good, (warnings or output[-1:] or [""])


def main():
    check_only = "--check" in sys.argv
    comp = iscc()
    if not check_only:
        print("downloading unofficial translations...")
        got = download()
        print("  %d files -> installer/lang/" % len(got))

    files = sorted(f for f in os.listdir(LANG) if f.lower().endswith((".isl", ".islu")))
    ok, bad = [], []
    for f in files:
        good, msg = compiles(comp, os.path.join(LANG, f))
        (ok if good else bad).append((f, msg[0] if msg else ""))
    print("\ncompiles: %d ok, %d dropped" % (len(ok), len(bad)))
    for f, msg in bad:
        print("  DROP %-28s %s" % (f, msg[:90]))
        os.rename(os.path.join(LANG, f), os.path.join(LANG, f + ".broken"))

    print("\n; --- paste into [Languages] (vendored, unofficial upstream translations) ---")
    for f, _ in ok:
        code = re.sub(r"\.islu?$", "", f)
        print('Name: "%s"; MessagesFile: "lang\\%s"' % (code.lower()[:12], f))
    print("\n; native LanguageName of each, for ordering overrides:")
    for f, _ in ok:
        print(";   %-28s %s" % (f, language_name(os.path.join(LANG, f))))


if __name__ == "__main__":
    main()
