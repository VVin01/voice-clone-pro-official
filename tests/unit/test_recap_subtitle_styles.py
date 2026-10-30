"""Tests for subtitle style presets (production-grade Feature 4)."""
import re

from app.services.recap_engine.core import (
    SUBTITLE_STYLE_PRESETS,
    write_synced_ass_file,
)

CUES = [(0.0, 2.0, "မင်္ဂလာပါ"), (2.0, 4.0, "ဒုတိယ စာကြောင်း")]


def _style_line(ass_text: str) -> str:
    for line in ass_text.splitlines():
        if line.startswith("Style: Default"):
            return line
    raise AssertionError("no Default style line in ASS output")


def test_existing_presets_unchanged():
    # The four historical presets keep their exact rendering fields.
    assert SUBTITLE_STYLE_PRESETS["yellow"]["primary"] == "&H0000FFFF"
    assert SUBTITLE_STYLE_PRESETS["yellow"]["border_style"] == 1
    assert SUBTITLE_STYLE_PRESETS["white"]["primary"] == "&H00FFFFFF"
    assert SUBTITLE_STYLE_PRESETS["boxed"]["border_style"] == 3
    assert SUBTITLE_STYLE_PRESETS["neon"]["primary"] == "&H00FFFF00"


def test_new_presets_registered_with_all_fields():
    for key in ("band", "heavy", "soft"):
        st = SUBTITLE_STYLE_PRESETS[key]
        for field in ("name", "label", "short", "primary", "outline_col",
                      "back_col", "border_style", "outline", "shadow"):
            assert field in st, f"{key} missing {field}"
    assert SUBTITLE_STYLE_PRESETS["band"]["border_style"] == 3  # band = boxed family
    assert SUBTITLE_STYLE_PRESETS["heavy"]["outline"] > SUBTITLE_STYLE_PRESETS["yellow"]["outline"]
    assert SUBTITLE_STYLE_PRESETS["soft"]["outline"] < SUBTITLE_STYLE_PRESETS["yellow"]["outline"]


def test_ass_header_uses_new_presets(tmp_path):
    for key in ("band", "heavy", "soft"):
        out = write_synced_ass_file(
            CUES, tmp_path / f"{key}.ass", 1080, 1920, sub_style=key,
        )
        line = _style_line(out.read_text(encoding="utf-8"))
        st = SUBTITLE_STYLE_PRESETS[key]
        assert st["primary"] in line
        assert st["back_col"] in line
        # BorderStyle column value matches the preset.
        m = re.search(r",(\d+),[\d.]+,[\d.]+,2,", line)
        assert m and int(m.group(1)) == st["border_style"], (key, line)


def test_unknown_style_still_falls_back_to_yellow(tmp_path):
    out = write_synced_ass_file(CUES, tmp_path / "x.ass", 1080, 1920, sub_style="bogus")
    line = _style_line(out.read_text(encoding="utf-8"))
    assert SUBTITLE_STYLE_PRESETS["yellow"]["primary"] in line


def test_default_style_is_yellow(tmp_path):
    out = write_synced_ass_file(CUES, tmp_path / "d.ass", 1080, 1920)
    line = _style_line(out.read_text(encoding="utf-8"))
    assert SUBTITLE_STYLE_PRESETS["yellow"]["primary"] in line
