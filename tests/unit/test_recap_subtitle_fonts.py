"""Tests for subtitle font options (recap-quality pass, 2026-10-30).

Fonts are bundled in app/assets/fonts/ under OFL-1.1; the option resolves
to a family name for the ASS writer and the files are staged into the
libass fontsdir so burns work on hosts without system Myanmar fonts.
"""
import re

from app.services.recap_engine.core import (
    SUBTITLE_FONT_OPTIONS,
    bundled_font_files,
    bundled_fonts_dir,
    resolve_subtitle_font,
    stage_bundled_fonts,
    write_synced_ass_file,
)

CUES = [(0.0, 2.0, "မင်္ဂလာပါ"), (2.0, 4.0, "ဒုတိယ စာကြောင်း")]


def _style_line(ass_text: str) -> str:
    for line in ass_text.splitlines():
        if line.startswith("Style: Default"):
            return line
    raise AssertionError("no Default style line in ASS output")


def test_registry_options_and_files_exist():
    assert set(SUBTITLE_FONT_OPTIONS) >= {"default", "serif", "padauk"}
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
    # "default" is the no-explicit-choice selector: since 2026-10-30 it
    # resolves to Padauk (DEFAULT_SUBTITLE_FONT), the production default.
    assert resolve_subtitle_font("default") == ("padauk", "Padauk")
    assert resolve_subtitle_font("serif") == ("serif", "Noto Serif Myanmar")
    assert resolve_subtitle_font("padauk") == ("padauk", "Padauk")
    # Family names round-trip (callers may pass either form) — an explicit
    # family name still reaches its own family, incl. Noto Sans Myanmar.
    assert resolve_subtitle_font("Noto Serif Myanmar") == ("serif", "Noto Serif Myanmar")
    assert resolve_subtitle_font("Padauk") == ("padauk", "Padauk")
    assert resolve_subtitle_font("Noto Sans Myanmar") == ("default", "Noto Sans Myanmar")
    # Legacy / unknown / empty all fall back to the default (Padauk).
    for legacy in ("Pyidaungsu", "pyidaungsu", "", None, "no-such-font"):
        assert resolve_subtitle_font(legacy) == ("padauk", "Padauk"), legacy


def test_ass_header_uses_chosen_family(tmp_path):
    # "default" selector -> Padauk (production default since 2026-10-30);
    # the explicit family name "Noto Sans Myanmar" still yields Noto Sans.
    for key, family in (("default", "Padauk"),
                        ("serif", "Noto Serif Myanmar"),
                        ("padauk", "Padauk"),
                        ("Noto Sans Myanmar", "Noto Sans Myanmar")):
        out = write_synced_ass_file(CUES, tmp_path / f"{key}.ass", 1280, 720, font_name=key)
        line = _style_line(out.read_text(encoding="utf-8"))
        assert f",{family}," in line, (key, line)


def test_default_ass_font_is_historical_family(tmp_path):
    out = write_synced_ass_file(CUES, tmp_path / "d.ass", 1280, 720)
    line = _style_line(out.read_text(encoding="utf-8"))
    assert ",Noto Sans Myanmar," in line


def test_stage_bundled_fonts_copies_everything(tmp_path):
    dest = stage_bundled_fonts(tmp_path / "fonts")
    assert dest is not None
    for fn in bundled_font_files():
        assert (dest / fn).is_file(), fn
    # All three families are represented in the staged dir.
    names = {p.name for p in dest.iterdir()}
    assert any("Serif" in n for n in names)
    assert any("Padauk" in n for n in names)
    assert "NotoSansMyanmar.ttf" in names
