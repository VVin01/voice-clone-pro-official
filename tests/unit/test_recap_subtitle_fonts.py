"""Tests for subtitle font options (recap-quality pass, 2026-10-30).

Fonts are bundled in app/assets/fonts/ under OFL-1.1; the option resolves
to a family name for the ASS writer and the files are staged into the
libass fontsdir so burns work on hosts without system Myanmar fonts.
"""
import re

from app.services.recap_engine.core import (
    SUBTITLE_FONT_OPTIONS,
    bundled_fonts_dir,
    resolve_subtitle_font,
    write_synced_ass_file,
)

CUES = [(0.0, 2.0, "မင်္ဂလာပါ"), (2.0, 4.0, "ဒုတိယ စာကြောင်း")]


def _style_line(ass_text: str) -> str:
    for line in ass_text.splitlines():
        if line.startswith("Style: Default"):
            return line
    raise AssertionError("no Default style line in ASS output")


def test_registry_options_and_files_exist():
    # Batch Q font choice (2026-11-04): exactly the four verified keys.
    assert set(SUBTITLE_FONT_OPTIONS) == {"padauk", "noto_sans", "pyidaungsu", "noto_serif"}
    fonts_dir = bundled_fonts_dir()
    for key, opt in SUBTITLE_FONT_OPTIONS.items():
        for field in ("name", "label", "short", "family", "files", "license"):
            assert field in opt, f"{key} missing {field}"
        for fn in opt["files"]:
            assert (fonts_dir / fn).is_file(), f"{key} font file missing: {fn}"
        # The license named in the registry must ship beside the fonts.
        m = re.search(r"\((LICENSE-[^)]+)\)", opt["license"])
        assert m and (fonts_dir / m.group(1)).is_file(), f"{key} license file missing"


def test_resolve_keys_families_and_legacy():
    # Batch Q font choice (2026-11-04): four family keys. The legacy
    # "default" selector was the old picker's Noto Sans Myanmar
    # label; Batch T (Ulfa, 2026-11-04) migrates those savers to
    # Padauk, so it aliases to padauk — same landing as the
    # un-chosen path (empty/unknown → DEFAULT_SUBTITLE_FONT, Padauk,
    # re-verified through the raqm PNG renderer). Explicit keys
    # (noto_sans below) keep their meaning.
    assert resolve_subtitle_font("default") == ("padauk", "Padauk")
    assert resolve_subtitle_font("padauk") == ("padauk", "Padauk")
    assert resolve_subtitle_font("noto_sans") == ("noto_sans", "Noto Sans Myanmar")
    assert resolve_subtitle_font("pyidaungsu") == ("pyidaungsu", "Pyidaungsu")
    assert resolve_subtitle_font("noto_serif") == ("noto_serif", "Noto Serif Myanmar")
    # Legacy key alias from stored configs.
    assert resolve_subtitle_font("serif") == ("noto_serif", "Noto Serif Myanmar")
    # Family names round-trip (callers may pass either form) — an explicit
    # family name still reaches its own family.
    assert resolve_subtitle_font("Noto Serif Myanmar") == ("noto_serif", "Noto Serif Myanmar")
    assert resolve_subtitle_font("Padauk") == ("padauk", "Padauk")
    assert resolve_subtitle_font("Noto Sans Myanmar") == ("noto_sans", "Noto Sans Myanmar")
    assert resolve_subtitle_font("Pyidaungsu") == ("pyidaungsu", "Pyidaungsu")
    # Unknown / empty fall back to the default (Padauk).
    for legacy in ("", None, "no-such-font"):
        assert resolve_subtitle_font(legacy) == ("padauk", "Padauk"), legacy


def test_ass_header_uses_chosen_family(tmp_path):
    # Explicit keys/families reach their own families in the ASS
    # side-file header.
    for key, family in (("padauk", "Padauk"),
                        ("noto_sans", "Noto Sans Myanmar"),
                        ("pyidaungsu", "Pyidaungsu"),
                        ("noto_serif", "Noto Serif Myanmar"),
                        ("Noto Sans Myanmar", "Noto Sans Myanmar")):
        out = write_synced_ass_file(CUES, tmp_path / f"{key}.ass", 1280, 720, font_name=key)
        line = _style_line(out.read_text(encoding="utf-8"))
        assert f",{family}," in line, (key, line)


def test_default_ass_font_is_historical_family(tmp_path):
    # The un-chosen default family is Padauk since the Batch Q font
    # choice (2026-11-04); before that it was Noto Sans Myanmar.
    out = write_synced_ass_file(CUES, tmp_path / "d.ass", 1280, 720)
    line = _style_line(out.read_text(encoding="utf-8"))
    assert ",Padauk," in line


def test_default_font_files_are_static_with_mym2_shaping():
    # Guard for the 2026-11-01 shaping fix: the production-default font
    # files must be STATIC builds (no fvar table) carrying Myanmar GSUB
    # shaping (mym2 script). Re-bundling a variable Noto build as the
    # default would silently restore the merged-stack defect.
    import pytest

    pytest.importorskip("fontTools")
    from fontTools.ttLib import TTFont

    from app.services.recap_engine.core import DEFAULT_SUBTITLE_FONT

    fonts_dir = bundled_fonts_dir()
    for fn in SUBTITLE_FONT_OPTIONS[DEFAULT_SUBTITLE_FONT]["files"]:
        f = TTFont(fonts_dir / fn, lazy=True)
        assert "fvar" not in f, f"{fn} is a variable font"
        assert "GSUB" in f, f"{fn} has no GSUB table"
        scripts = [r.ScriptTag for r in f["GSUB"].table.ScriptList.ScriptRecord]
        assert "mym2" in scripts, f"{fn} lacks the mym2 shaping script"
