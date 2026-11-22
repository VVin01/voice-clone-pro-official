"""Batch Q font choice (Ulfa, 2026-11-04): user-selectable subtitle font.

Four options, every one shaping-verified through the Pillow+raqm
PNG renderer before adoption (font-compare session): `padauk`
(DEFAULT), `noto_sans`, `pyidaungsu`, `noto_serif`. Pins:

* the registry carries EXACTLY these four keys, Padauk is the
  default, every option's files + OFL licence ship in the bundled
  assets, and the variable font is in no option's file list;
* config validation accepts only the four keys (legacy selectors
  and family names resolve; unknown values land on the default);
* the PNG renderer loads the chosen family BY PATH and the choice
  separates the PNG cache;
* a missing font file falls back to noto_sans with a log record —
  never a failed job;
* the Mini App picker, i18n labels, snapshot enum, submit payload
  and the @font-face webfonts are wired for the same four keys.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = ROOT / "app" / "services" / "recap_engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import core  # noqa: E402
import png_subtitles as pq  # noqa: E402
from core import (  # noqa: E402
    DEFAULT_SUBTITLE_FONT,
    SUBTITLE_FONT_OPTIONS,
    bundled_fonts_dir,
    resolve_subtitle_font,
    subtitle_font_file,
)

APP_JS = (ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app" / "webapp" / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app" / "webapp" / "static" / "css" / "style.css").read_text(encoding="utf-8")

KEYS = ("padauk", "noto_sans", "pyidaungsu", "noto_serif")
FILES = {
    "padauk": "Padauk-Bold.ttf",
    "noto_sans": "NotoSansMyanmar-Bold.ttf",
    "pyidaungsu": "Pyidaungsu-Bold.ttf",
    "noto_serif": "NotoSerifMyanmar-Bold.ttf",
}
FAMILIES = {
    "padauk": "Padauk",
    "noto_sans": "Noto Sans Myanmar",
    "pyidaungsu": "Pyidaungsu",
    "noto_serif": "Noto Serif Myanmar",
}


# ── 1. Registry shape ──────────────────────────────────────────────

def test_registry_is_exactly_the_four_verified_options():
    assert set(SUBTITLE_FONT_OPTIONS) == set(KEYS)
    assert DEFAULT_SUBTITLE_FONT == "padauk"
    fonts_dir = bundled_fonts_dir()
    for key, opt in SUBTITLE_FONT_OPTIONS.items():
        assert opt["family"] == FAMILIES[key]
        for fn in opt["files"]:
            assert (fonts_dir / fn).is_file(), f"{key}: {fn} missing"
            assert fn != "NotoSansMyanmar.ttf"  # variable build stays banned
        m = re.search(r"\((LICENSE-[^)]+)\)", opt["license"])
        assert m and (fonts_dir / m.group(1)).is_file(), f"{key}: licence missing"


def test_resolve_keys_families_aliases_and_default():
    for key in KEYS:
        assert resolve_subtitle_font(key) == (key, FAMILIES[key])
        assert resolve_subtitle_font(FAMILIES[key]) == (key, FAMILIES[key])
    # Legacy selectors from stored configs: "Serif" was an explicit
    # choice of Noto Serif Myanmar and keeps its meaning; "Default"
    # was the old factory value and — since Batch T (Ulfa,
    # 2026-11-04) — migrates to the Padauk default.
    assert resolve_subtitle_font("serif") == ("noto_serif", "Noto Serif Myanmar")
    assert resolve_subtitle_font("default") == ("padauk", "Padauk")
    # Un-chosen / unknown → the Padauk default.
    for v in ("", None, "ComicSansUnknown"):
        assert resolve_subtitle_font(v) == ("padauk", "Padauk"), v


# ── 2. Config validation: only the four keys ───────────────────────

def test_recap_config_accepts_only_the_four_keys():
    from app.webapp.api import RecapConfigRequest

    for key in KEYS:
        req = RecapConfigRequest(url="https://example.com/v.mp4", sub_font=key)
        assert req.sub_font == key
    # Default when omitted is the product default.
    assert RecapConfigRequest(url="https://example.com/v.mp4").sub_font == "padauk"
    # Aliases/families resolve; unknown degrades to the default
    # instead of failing the submit (validator contract).
    assert RecapConfigRequest(
        url="https://example.com/v.mp4", sub_font="serif"
    ).sub_font == "noto_serif"
    assert RecapConfigRequest(
        url="https://example.com/v.mp4", sub_font="default"
    ).sub_font == "padauk"
    assert RecapConfigRequest(
        url="https://example.com/v.mp4", sub_font="Pyidaungsu"
    ).sub_font == "pyidaungsu"
    assert RecapConfigRequest(
        url="https://example.com/v.mp4", sub_font="ComicSansUnknown"
    ).sub_font == "padauk"


# ── 3. Renderer: font by path + cache separation ───────────────────

def test_font_file_per_key_is_the_bold_build():
    for key in KEYS:
        p = subtitle_font_file(key)
        assert p.name == FILES[key]
        assert p.is_file()


def test_renderer_cache_separates_fonts(tmp_path):
    r = pq.PngSubtitleRenderer(tmp_path / "cache")
    v = pq.preset_visuals(core.SUBTITLE_STYLE_PRESETS["yellow"], 720, 29)
    outs = {}
    for key in KEYS:
        out = r.render_text("မြို့ကြီးဆီကို ထွက်ခွာပြီး", 1, 29, subtitle_font_file(key), v)
        assert out is not None, key
        outs[key] = out
    paths = {k: o[0] for k, o in outs.items()}
    assert len(set(paths.values())) == 4  # one cache entry per font
    # Different shaping → at least the widths are not all identical.
    widths = {o[1] for o in outs.values()}
    assert len(widths) >= 2


# ── 4. Missing file → noto_sans fallback, logged, never fatal ──────

def test_missing_font_file_falls_back_to_noto_sans(tmp_path, monkeypatch, caplog):
    # A fonts dir holding ONLY the noto_sans Bold build.
    only = tmp_path / "fonts"
    only.mkdir()
    (only / "NotoSansMyanmar-Bold.ttf").write_bytes(
        (bundled_fonts_dir() / "NotoSansMyanmar-Bold.ttf").read_bytes()
    )
    monkeypatch.setattr(core, "bundled_fonts_dir", lambda: only)
    with caplog.at_level(logging.WARNING):
        p = subtitle_font_file("pyidaungsu")
    assert p.name == "NotoSansMyanmar-Bold.ttf"
    assert "falling back to noto_sans" in caplog.text


# ── 5. Mini App wiring ─────────────────────────────────────────────

def test_picker_has_the_four_options_with_padauk_active():
    block = re.search(
        r'id="recap-sub-font-picker"[^>]*>(.*?)</select>', INDEX_HTML, re.DOTALL
    )
    assert block is not None
    body = block.group(1)
    for key in KEYS:
        assert f'data-font="{key}"' in body
    m = re.search(r'<option[^>]*value="padauk"[^>]*>', body)
    assert m is not None and "selected" in m.group(0)
    assert 'value="custom"' in body
    assert 'data-font="default"' not in body
    assert 'data-font="serif"' not in body


def test_appjs_wiring_uses_the_four_keys():
    assert 'recapSubFont: "padauk"' in APP_JS
    assert 'recapSubFont: ["padauk", "noto_sans", "pyidaungsu", "noto_serif"]' in APP_JS
    assert 'sub_font: state.recapSubFont || "padauk"' in APP_JS
    # Batch AK: the JS preview fontMap is retired with the CSS
    # preview — the engine resolves the font for the composited
    # frame; every key must resolve to its bundled file.
    for key in FAMILIES:
        assert subtitle_font_file(key).name == FILES[key]
    # Saved snapshots from before the rename are migrated on
    # restore: "Serif" keeps its font; "Default" savers move to
    # Padauk (Batch T, Ulfa 2026-11-04).
    assert 'LEGACY_SUB_FONTS = { "default": "padauk", "serif": "noto_serif" }' in APP_JS


def test_i18n_labels_for_all_four_in_both_locales():
    for key in (
        "recap_sub_font_padauk",
        "recap_sub_font_noto_sans",
        "recap_sub_font_pyidaungsu",
        "recap_sub_font_noto_serif",
    ):
        assert I18N_JS.count(f"{key}:") == 2, key  # my + en


def test_webfonts_served_for_all_four_families():
    static_fonts = ROOT / "app" / "webapp" / "static" / "fonts"
    for key in KEYS:
        assert (static_fonts / FILES[key]).is_file(), key
        assert f"url('/static/fonts/{FILES[key]}')" in STYLE_CSS
        assert f"font-family: '{FAMILIES[key]}';" in STYLE_CSS


def test_default_ass_family_is_padauk(tmp_path):
    out = core.write_synced_ass_file(
        [(0.0, 2.0, "မင်္ဂလာပါ")], tmp_path / "d.ass", 1280, 720
    )
    line = next(
        l for l in out.read_text(encoding="utf-8").splitlines()
        if l.startswith("Style: Default")
    )
    assert ",Padauk," in line


# ── 7. Batch T: "Default" savers migrate to Padauk ─────────────────

def test_batch_t_default_alias_is_padauk_at_every_boundary():
    """Batch T (Ulfa, 2026-11-04): the legacy "default" selector
    resolves to Padauk at EVERY boundary — the server alias map, the
    renderer resolve, the font FILE the PNG renderer loads, the
    config validator, and the Mini App snapshot-restore map (which
    history prefill also funnels through). Explicit choices are
    untouched: a stored "noto_sans" still renders Noto Sans and the
    "serif" alias still means Noto Serif."""
    from app.webapp.api import RecapConfigRequest

    # Server alias map + renderer resolve.
    assert core.SUBTITLE_FONT_ALIASES == {"default": "padauk", "serif": "noto_serif"}
    assert resolve_subtitle_font("default") == ("padauk", "Padauk")
    assert resolve_subtitle_font("Default") == ("padauk", "Padauk")
    # The file actually loaded for a "default"-valued job is Padauk's.
    assert subtitle_font_file("default").name == "Padauk-Bold.ttf"
    # Config validator boundary.
    assert RecapConfigRequest(
        url="https://example.com/v.mp4", sub_font="default"
    ).sub_font == "padauk"
    # Mini App snapshot restore / history prefill boundary.
    assert 'LEGACY_SUB_FONTS = { "default": "padauk", "serif": "noto_serif" }' in APP_JS
    # Explicit choices keep their meaning.
    assert resolve_subtitle_font("noto_sans") == ("noto_sans", "Noto Sans Myanmar")
    assert subtitle_font_file("noto_sans").name == "NotoSansMyanmar-Bold.ttf"
    assert resolve_subtitle_font("serif") == ("noto_serif", "Noto Serif Myanmar")
    assert RecapConfigRequest(
        url="https://example.com/v.mp4", sub_font="noto_sans"
    ).sub_font == "noto_sans"
