"""Generate the installer's language wiring from the .isl files actually present — one source of truth.

Reads Inno's bundled Languages folder + our vendored installer/lang/, and emits two #include files:

  languages.iss   the [Languages] section (every language, so /LANG=xx relaunch + chrome work)
  langdata.iss    a [Code] fragment: AddChip()/AddMore() calls (top 12 as chips, the rest in the
                  "more" dropdown) and CollieLang() mapping installer code -> Collie UI-language code

Ordering is decided HERE, in code — which is the whole reason the fancy page replaced Inno's native
"Select Setup Language" combo: that combo force-sorts alphabetically by native name (so CJK always
sank to the bottom), and there was no way to say "put 简体中文 near the top". A custom page draws them
in whatever order we choose. So: a hand-picked top-12 by real-world prevalence, then everything else
alphabetized by English name in the dropdown.

    python installer/gen_langs.py        # regenerate both includes
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
VEND = os.path.join(HERE, "lang")


def _find_inno():
    """Inno's install dir, wherever it landed: per-user (winget/manual) or machine (choco on CI)."""
    cands = [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6"),
             os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Inno Setup 6"),
             os.path.join(os.environ.get("ProgramFiles", ""), "Inno Setup 6")]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "Default.isl")):
            return c
    return cands[0]   # best-effort; missing files surface as a clear warning later


INNO = _find_inno()
BUNDLED = os.path.join(INNO, "Languages")

# The chip row: prevalence-ordered, Simplified Chinese deliberately second (right after English) as
# the user asked. These MUST be codes we emit in [Languages].
CHIPS = ["en", "zh", "zhtw", "es", "fr", "de", "ptbr", "ru", "ja", "ko", "it", "ar"]

# The "more" dropdown. 77 languages was too many — a long tail of tiny-audience, lagging unofficial
# translations (Abkhazian, Corsican, Ewe, Ligurian, Occitan, Valencian...). This is a curated set of
# the highest-population world languages that also have a solid Inno translation. Chips + these ≈ 30,
# which covers the overwhelming majority of users without the noise. To offer a language, add its
# code here (and make sure gen has an .isl for it). Order here doesn't matter — sorted by English
# name at emit time.
MORE = ["hi", "id", "vi", "tr", "pl", "nl", "th", "uk", "cs", "sv", "el", "ro", "hu", "fi", "da",
        "he", "no", "bg", "sk", "fa", "ta"]

# Installer language code -> Collie's own UI-language code. Only the languages collie's GUI is
# actually translated into map to themselves; everything else follows the browser ("auto"), which
# is what an untranslated UI should do. Kept in sync with settings.py SCHEMA[LANG].options.
COLLIE = {"en": "en", "zh": "zh", "zhtw": "zh-tw", "es": "es", "fr": "fr", "de": "de",
          "ptbr": "pt", "pt": "pt", "ru": "ru", "ja": "ja", "ko": "ko"}

# Shorter native labels where the .isl's own name is too long for a chip.
NATIVE = {"ptbr": "Português", "englishbritish": "English (UK)"}

# Nice English display names where the filename alone is ambiguous.
ENGLISH = {
    "en": "English", "zh": "Chinese (Simplified)", "zhtw": "Chinese (Traditional)",
    "ptbr": "Portuguese (Brazil)", "pt": "Portuguese", "enus": "English (US)",
    "englishbritish": "English (UK)", "norwegiannyn": "Norwegian (Nynorsk)",
    "serbiancyril": "Serbian (Cyrillic)", "serbianlatin": "Serbian (Latin)",
    "scottishgael": "Scottish Gaelic", "chinesetradi": "Chinese (Traditional)",
}

# Filename (stem) -> our short ISO installer code, for BOTH the bundled and vendored sets so the
# CHIPS/MORE lists can use clean codes like "vi"/"el" instead of "vietnamese"/"greek". Anything not
# listed falls back to stem.lower()[:12].
STEM_CODE = {
    # bundled (Inno's own Languages folder + Default.isl = English)
    "Default": "en", "ChineseSimplified": "zh", "Japanese": "ja", "Korean": "ko",
    "Spanish": "es", "French": "fr", "German": "de", "Portuguese": "pt",
    "BrazilianPortuguese": "ptbr", "Russian": "ru", "Arabic": "ar", "Armenian": "hy",
    "Bulgarian": "bg", "Catalan": "ca", "Corsican": "co", "Czech": "cs", "Danish": "da",
    "Dutch": "nl", "Finnish": "fi", "Hebrew": "he", "Hungarian": "hu", "Italian": "it",
    "Norwegian": "no", "Polish": "pl", "Slovak": "sk", "Slovenian": "sl",
    "Swedish": "sv", "Tamil": "ta", "Thai": "th", "Turkish": "tr", "Ukrainian": "uk",
    # vendored (unofficial upstream + Traditional Chinese)
    "ChineseTraditional": "zhtw", "Hindi": "hi", "Indonesian": "id", "Vietnamese": "vi",
    "Greek": "el", "Romanian": "ro", "Farsi": "fa",
}


def decode(name):
    """Turn a LanguageName like '<65E5><672C><8A9E>' into real Unicode for a [Code] literal."""
    return re.sub(r"<([0-9A-Fa-f]{4})>", lambda m: chr(int(m.group(1), 16)), name)


def native_of(path):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            txt = open(path, encoding=enc).read()
        except (UnicodeDecodeError, LookupError):
            continue
        m = re.search(r"^LanguageName=(.*)$", txt, re.M)
        if m:
            return decode(m.group(1).strip())
    return None


def english_of(code, stem):
    if code in ENGLISH:
        return ENGLISH[code]
    # split CamelCase filename into words
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem)
    return words


def collect():
    """Return list of dicts {code, native, english, msgfile} for every available language."""
    out, seen = [], set()

    def add(code, native, english, msgfile):
        if not code or code in seen or not native:
            return
        seen.add(code)
        out.append({"code": code, "native": NATIVE.get(code, native),
                    "english": english, "msgfile": msgfile})

    # bundled
    add("en", native_of(os.path.join(INNO, "Default.isl")) or "English", "English",
        "compiler:Default.isl")
    if os.path.isdir(BUNDLED):
        for f in sorted(os.listdir(BUNDLED)):
            if not f.lower().endswith(".isl"):
                continue
            stem = f[:-4]
            code = STEM_CODE.get(stem, stem.lower()[:12])
            add(code, native_of(os.path.join(BUNDLED, f)), english_of(code, stem),
                "compiler:Languages\\" + f)
    # vendored (unofficial + Traditional Chinese)
    if os.path.isdir(VEND):
        for f in sorted(os.listdir(VEND)):
            if not f.lower().endswith((".isl", ".islu")):
                continue
            stem = re.sub(r"\.islu?$", "", f)
            code = STEM_CODE.get(stem, stem.lower()[:12])
            add(code, native_of(os.path.join(VEND, f)), english_of(code, stem), "lang\\" + f)

    # keep only the curated set (chips + dropdown); the rest are available as .isl files but not
    # offered — trimming the 77 discovered languages down to ~30 the user will actually recognize.
    keep = set(CHIPS) | set(MORE)
    missing = keep - {L["code"] for L in out}
    if missing:
        print("WARNING: curated codes with no .isl available:", ", ".join(sorted(missing)))
    return [L for L in out if L["code"] in keep]


def emit_languages(langs):
    lines = ["; AUTO-GENERATED by gen_langs.py — do not edit. %d languages." % len(langs),
             "; The native Select-Language dialog is disabled (ShowLanguageDialog=no); these exist so",
             "; /LANG=xx relaunch works and each wizard renders in its own translation."]
    for L in langs:
        lines.append('Name: "%s"; MessagesFile: "%s"' % (L["code"], L["msgfile"]))
    return "\n".join(lines) + "\n"


def emit_langdata(langs):
    by_code = {L["code"]: L for L in langs}
    chips = [by_code[c] for c in CHIPS if c in by_code]
    chip_codes = {c["code"] for c in chips}
    # only MORE goes in the dropdown, alphabetized by English name
    rest = sorted((by_code[c] for c in MORE if c in by_code and c not in chip_codes),
                  key=lambda L: L["english"].lower())

    def esc(s):
        return s.replace("'", "''")

    out = ["{ AUTO-GENERATED by gen_langs.py — do not edit. }",
           "procedure BuildLanguageList;", "begin"]
    for L in chips:
        out.append("  AddChip('%s', '%s', '%s');" % (esc(L["native"]), esc(L["english"]), L["code"]))
    for L in rest:
        out.append("  AddMore('%s', '%s', '%s');" % (esc(L["native"]), esc(L["english"]), L["code"]))
    out.append("end;")
    out.append("")
    out.append("function CollieLang(const Code: String): String;")
    out.append("begin")
    out.append("  Result := 'auto';")
    for ic, cc in COLLIE.items():
        if ic in by_code:
            out.append("  if CompareText(Code, '%s') = 0 then Result := '%s';" % (ic, cc))
    out.append("end;")
    return "\n".join(out) + "\n"


def main():
    langs = collect()
    with open(os.path.join(HERE, "languages.iss"), "wb") as f:
        f.write(b"\xef\xbb\xbf" + emit_languages(langs).encode("utf-8"))
    with open(os.path.join(HERE, "langdata.iss"), "wb") as f:
        f.write(b"\xef\xbb\xbf" + emit_langdata(langs).encode("utf-8"))
    print("%d languages -> languages.iss + langdata.iss" % len(langs))
    print("  chips:", ", ".join(c for c in CHIPS))
    print("  collie-localized:", ", ".join(sorted(set(COLLIE.values()))))


if __name__ == "__main__":
    main()
