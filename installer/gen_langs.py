"""Generate the installer's language wiring from the .isl files actually present — one source of truth.

Reads Inno's bundled Languages folder + our vendored installer/lang/, and emits two #include files:

  languages.iss   the [Languages] section (every language, so /LANG=xx relaunch + chrome work)
  langdata.iss    a [Code] fragment: AddChip()/AddMore() calls (top 12 as chips, the rest in the
                  "more" dropdown) and CollieLang() mapping installer code -> Collie UI-language code

Vendored translations do not necessarily move in lockstep with Inno Setup. Before they are wired
in, this generator also writes ``lang_compat/*.islu`` against the installed compiler's current
``Default.isl`` schema. Existing translations are retained, obsolete keys are omitted, and new
messages use Inno's official English text. That is the same fallback Inno performs itself, but made
explicit so a release compile stays warning-clean without inventing translations.

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
COMPAT = os.path.join(HERE, "lang_compat")


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

_REQUIRED_LANG_OPTIONS = {"languagename", "languageid", "languagecodepage"}
_POSITIONAL_PARAMETER = re.compile(r"%[0-9]+")

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


def _read_language(path):
    """Read an Inno language file without assuming official/unofficial source encoding."""
    with open(path, "rb") as f:
        data = f.read()
    encodings = ["utf-8-sig"]
    # Legacy .isl files are ANSI and declare their decoder in an otherwise-ASCII LangOptions line.
    # Honor that declaration before generic Western fallbacks; Romanian cp1250, Farsi cp1256, etc.
    codepage = re.search(br"(?im)^\s*LanguageCodePage\s*=\s*([0-9]+)\s*$", data)
    if codepage and codepage.group(1) != b"0":
        encodings.append("cp" + codepage.group(1).decode("ascii"))
    encodings.extend(("cp1252", "latin-1"))
    for enc in encodings:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise UnicodeError("could not decode Inno language file: %s" % path)


def _assignments(text, section, include_commented=False):
    """Return case-insensitive ``key -> (source spelling, value)`` for one .isl section."""
    current = ""
    found = {}
    for raw in text.splitlines():
        heading = re.match(r"^\s*\[([^]]+)\]\s*$", raw)
        if heading:
            current = heading.group(1).lower()
            continue
        if current != section.lower():
            continue
        line = raw.strip()
        if line.startswith(";"):
            if not include_commented:
                continue
            line = line[1:].strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.match(r"^[A-Za-z][A-Za-z0-9]*$", key):
            continue
        found[key.lower()] = (key, value)
    return found


def _option_schema(default_text, bundled_dir=None):
    """Derive valid LangOptions from the current compiler, including RTL-only directives."""
    options = _assignments(default_text, "LangOptions", include_commented=True)
    if bundled_dir and os.path.isdir(bundled_dir):
        for name in os.listdir(bundled_dir):
            if not name.lower().endswith((".isl", ".islu")):
                continue
            for lower, pair in _assignments(
                    _read_language(os.path.join(bundled_dir, name)), "LangOptions").items():
                options.setdefault(lower, pair)
    return options


def _compatible_translation(english, translated):
    """Do not reuse an old message if its positional substitution contract changed."""
    return set(_POSITIONAL_PARAMETER.findall(english)) == set(
        _POSITIONAL_PARAMETER.findall(translated))


def normalize_vendor_language(source, default, destination, bundled_dir=None):
    """Write one warning-clean Unicode language file against the current Inno message schema.

    Missing or parameter-incompatible messages deliberately receive the exact English value from
    ``Default.isl``. The result matches Inno's documented fallback behavior while making that
    fallback explicit enough for warning-free release builds.
    """
    default_text = _read_language(default)
    source_text = _read_language(source)
    source_options = _assignments(source_text, "LangOptions")
    missing_options = _REQUIRED_LANG_OPTIONS - set(source_options)
    if missing_options:
        raise ValueError("%s is missing required LangOptions: %s" %
                         (os.path.basename(source), ", ".join(sorted(missing_options))))

    valid_options = _option_schema(default_text, bundled_dir)
    kept_options = [pair for lower, pair in source_options.items() if lower in valid_options]
    dropped_options = sorted(pair[0] for lower, pair in source_options.items()
                             if lower not in valid_options)

    default_messages = _assignments(default_text, "Messages")
    source_messages = _assignments(source_text, "Messages")
    if not default_messages:
        raise ValueError("current Default.isl has no [Messages] schema: %s" % default)

    rendered_messages = []
    fallback = []
    incompatible = []
    for lower, (key, english) in default_messages.items():
        candidate = source_messages.get(lower)
        if candidate and _compatible_translation(english, candidate[1]):
            rendered_messages.append((key, candidate[1]))
        else:
            rendered_messages.append((key, english))
            fallback.append(key)
            if candidate:
                incompatible.append(key)

    default_custom = _assignments(default_text, "CustomMessages")
    source_custom = _assignments(source_text, "CustomMessages")
    rendered_custom = []
    for lower, (key, english) in default_custom.items():
        candidate = source_custom.get(lower)
        value = (candidate[1]
                 if candidate and _compatible_translation(english, candidate[1]) else english)
        rendered_custom.append((key, value))

    lines = [
        "; AUTO-GENERATED by gen_langs.py from %s; do not edit." % os.path.basename(source),
        "; Missing/current-version messages intentionally use the installed Inno Default.isl text.",
        "", "[LangOptions]",
    ]
    lines.extend("%s=%s" % pair for pair in kept_options)
    lines.extend(("", "[Messages]"))
    lines.extend("%s=%s" % pair for pair in rendered_messages)
    if rendered_custom:
        lines.extend(("", "[CustomMessages]"))
        lines.extend("%s=%s" % pair for pair in rendered_custom)
    lines.append("")

    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, "wb") as f:
        f.write(b"\xef\xbb\xbf" + "\r\n".join(lines).encode("utf-8"))

    obsolete = sorted(pair[0] for lower, pair in source_messages.items()
                      if lower not in default_messages)
    return {"translated": len(default_messages) - len(fallback),
            "fallback": fallback, "incompatible": incompatible, "obsolete": obsolete,
            "dropped_options": dropped_options}


def native_of(path):
    try:
        txt = _read_language(path)
    except (OSError, UnicodeError):
        return None
    else:
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

    def add(code, native, english, msgfile, source=None):
        if not code or code in seen or not native:
            return
        seen.add(code)
        out.append({"code": code, "native": NATIVE.get(code, native),
                    "english": english, "msgfile": msgfile, "source": source})

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
            source = os.path.join(VEND, f)
            add(code, native_of(source), english_of(code, stem), "lang\\" + f, source=source)

    # keep only the curated set (chips + dropdown); the rest are available as .isl files but not
    # offered — trimming the 77 discovered languages down to ~30 the user will actually recognize.
    keep = set(CHIPS) | set(MORE)
    missing = keep - {L["code"] for L in out}
    if missing:
        print("WARNING: curated codes with no .isl available:", ", ".join(sorted(missing)))
    selected = [L for L in out if L["code"] in keep]
    default = os.path.join(INNO, "Default.isl")
    for language in selected:
        if not language["source"]:
            continue
        stem = os.path.splitext(os.path.basename(language["source"]))[0]
        target_name = stem + ".islu"
        target = os.path.join(COMPAT, target_name)
        language["compat"] = normalize_vendor_language(
            language["source"], default, target, bundled_dir=BUNDLED)
        language["msgfile"] = "lang_compat\\" + target_name
    return selected


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
    for language in langs:
        stats = language.get("compat")
        if stats:
            print("  compat %-5s translated=%d english-fallback=%d parameter-mismatch=%d "
                  "obsolete=%d options-dropped=%d" %
                  (language["code"],
                   stats["translated"],
                   len(stats["fallback"]), len(stats["incompatible"]), len(stats["obsolete"]),
                   len(stats["dropped_options"])))


if __name__ == "__main__":
    main()
