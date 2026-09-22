"""Portable contracts for Inno's generated, warning-clean translation compatibility layer."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "collie_installer_gen_langs", ROOT / "installer" / "gen_langs.py")
assert SPEC and SPEC.loader
gen_langs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gen_langs)


DEFAULT = """\
[LangOptions]
LanguageName=English
LanguageID=$0409
LanguageCodePage=0
;DialogFontName=

[Messages]
Stable=English stable %1
NewMessage=New English text
ChangedContract=English value %1

[CustomMessages]
CustomStable=English custom %1
CustomNew=New English custom
"""


SOURCE = """\
[LangOptions]
LanguageName=Test language
LanguageID=$1234
LanguageCodePage=65001
DialogFontName=Test Font
RightToLeft=yes
TitleFontSize=35

[Messages]
Stable=Translated stable %1
ChangedContract=Unsafe old value %2
ObsoleteMessage=Old translated text

[CustomMessages]
CustomStable=Translated custom %1
ObsoleteCustom=Unused custom text
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_normalizer_uses_current_default_schema_without_inventing_translations(tmp_path):
    default = tmp_path / "inno" / "Default.isl"
    bundled = tmp_path / "inno" / "Languages"
    source = tmp_path / "source" / "Test.islu"
    output = tmp_path / "compat" / "Test.islu"
    _write(default, DEFAULT)
    _write(bundled / "Arabic.isl", """\
[LangOptions]
LanguageName=Arabic
LanguageID=$0401
LanguageCodePage=1256
RightToLeft=yes
[Messages]
""")
    _write(source, SOURCE)

    stats = gen_langs.normalize_vendor_language(
        str(source), str(default), str(output), bundled_dir=str(bundled))
    generated = output.read_text(encoding="utf-8-sig")

    assert "LanguageName=Test language" in generated
    assert "DialogFontName=Test Font" in generated
    assert "RightToLeft=yes" in generated
    assert "TitleFontSize" not in generated
    assert "Stable=Translated stable %1" in generated
    assert "NewMessage=New English text" in generated
    assert "ChangedContract=English value %1" in generated
    assert "ObsoleteMessage" not in generated
    assert "CustomStable=Translated custom %1" in generated
    assert "CustomNew=New English custom" in generated
    assert "ObsoleteCustom" not in generated
    assert stats["fallback"] == ["NewMessage", "ChangedContract"]
    assert stats["incompatible"] == ["ChangedContract"]
    assert stats["obsolete"] == ["ObsoleteMessage"]
    assert stats["dropped_options"] == ["TitleFontSize"]


def test_normalizer_honors_legacy_language_code_page(tmp_path):
    default = tmp_path / "inno" / "Default.isl"
    source = tmp_path / "source" / "Romanian.isl"
    output = tmp_path / "compat" / "Romanian.islu"
    _write(default, DEFAULT)
    romanian = SOURCE.replace("Test language", "Română").replace(
        "LanguageCodePage=65001", "LanguageCodePage=1250").replace(
        "Translated stable", "Traducere din ţară")
    source.parent.mkdir(parents=True)
    source.write_bytes(romanian.encode("cp1250"))

    gen_langs.normalize_vendor_language(str(source), str(default), str(output))
    generated = output.read_text(encoding="utf-8-sig")

    assert "LanguageName=Română" in generated
    assert "Stable=Traducere din ţară %1" in generated


def test_collect_routes_curated_vendored_languages_through_generated_compat_files(
        tmp_path, monkeypatch):
    inno = tmp_path / "inno"
    vend = tmp_path / "lang"
    compat = tmp_path / "lang_compat"
    _write(inno / "Default.isl", DEFAULT)
    (inno / "Languages").mkdir(parents=True)
    _write(vend / "Hindi.islu", SOURCE)

    monkeypatch.setattr(gen_langs, "INNO", str(inno))
    monkeypatch.setattr(gen_langs, "BUNDLED", str(inno / "Languages"))
    monkeypatch.setattr(gen_langs, "VEND", str(vend))
    monkeypatch.setattr(gen_langs, "COMPAT", str(compat))
    monkeypatch.setattr(gen_langs, "CHIPS", ["en", "hi"])
    monkeypatch.setattr(gen_langs, "MORE", [])

    languages = gen_langs.collect()
    hindi = next(language for language in languages if language["code"] == "hi")

    assert hindi["msgfile"] == r"lang_compat\Hindi.islu"
    assert (compat / "Hindi.islu").is_file()
    assert r'MessagesFile: "lang_compat\Hindi.islu"' in gen_langs.emit_languages(languages)


def test_installer_build_fails_closed_on_compiler_warnings():
    script = (ROOT / "installer" / "build.ps1").read_text(encoding="utf-8")
    assert "compilerWarnings" in script
    assert "iscc emitted" in script
    assert "gen_langs.py compatibility handling" in script
