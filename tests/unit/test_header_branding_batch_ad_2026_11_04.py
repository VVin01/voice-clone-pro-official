"""Batch AD pins (2026-11-04): Mini App header branding.

Owner complaint (screenshot, verified firsthand): the header brand line
read the word PRO twice — the title text "VOICE CLONE PRO" plus the blue
pill badge "PRO" beside it — and the in-app hexagon crest did not match
the bot's Telegram avatar.

Owner-corrected form (final): KEEP the blue badge, drop PRO from the
text name. The brand line is now, in order:

    logo + "VOICE CLONE" (text) + blue pill badge "PRO"
    "STUDIO" letterspaced subtitle underneath (retained)

Logo: the inline hexagon SVG crest is replaced by the bot's actual
Telegram profile artwork (microphone + sound waves), shipped as
app/webapp/static/img/brand-logo.svg — an SVG wrapper embedding the
avatar JPEG (the GitHub connector push path is text-only, so the JPEG
travels as base64 inside the SVG; the artwork bytes are the avatar's).
Static cache token r55 -> r56.
"""
from __future__ import annotations

import base64
import io
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
I18N_JS = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
LOGO_SVG_PATH = STATIC / "img" / "brand-logo.svg"


def _brand_block() -> str:
    start = INDEX_HTML.index('<div class="brand">')
    end = INDEX_HTML.index('<div class="top-actions">')
    return INDEX_HTML[start:end]


def test_brand_title_has_no_pro_in_either_locale():
    assert I18N_JS.count('brand_title: "VOICE CLONE"') == 2
    assert 'brand_title: "VOICE CLONE PRO"' not in I18N_JS


def test_brand_block_reads_voice_clone_plus_pro_badge():
    block = _brand_block()
    # Text name without PRO, exactly as the owner ordered.
    assert '<h1 data-i18n="brand_title">VOICE CLONE</h1>' in block
    assert "VOICE CLONE PRO" not in block
    # The blue pill badge stays, exactly once.
    assert block.count('<span class="pro-tag">PRO</span>') == 1
    # STUDIO subtitle retained.
    assert '<span class="sub-brand" data-i18n="brand_subtitle">STUDIO</span>' in block


def test_header_logo_is_the_telegram_avatar_asset():
    block = _brand_block()
    assert 'src="/static/img/brand-logo.svg?v=20261104_r66"' in block
    assert 'class="brand-logo-img"' in block
    # The old hand-drawn hexagon crest is gone from the header.
    assert "crest-svg" not in INDEX_HTML
    assert "crest-grad" not in INDEX_HTML


def test_logo_asset_embeds_the_avatar_jpeg():
    assert LOGO_SVG_PATH.exists()
    svg = LOGO_SVG_PATH.read_text(encoding="utf-8")
    match = re.search(r"data:image/jpeg;base64,([A-Za-z0-9+/=]+)", svg)
    assert match, "brand-logo.svg must embed the avatar JPEG"
    raw = base64.b64decode(match.group(1))
    assert raw[:2] == b"\xff\xd8"  # JPEG magic
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as img:
        assert img.format == "JPEG"
        assert img.size == (192, 192)


def test_logo_img_css_is_a_clean_circle():
    rule = re.search(r"\.brand-logo-img\s*\{([^}]+)\}", STYLE_CSS)
    assert rule, ".brand-logo-img rule missing"
    body = rule.group(1)
    assert "border-radius: 50%" in body
    assert "width: 36px" in body and "height: 36px" in body


def test_static_cache_token_is_r56():
    assert "style.css?v=20261104_r66" in INDEX_HTML
    assert "20261104_r55" not in INDEX_HTML
    # Four versioned refs now: style.css, app.js, i18n.js and the new
    # brand-logo.svg. (The per-batch cache pins that total the token's
    # occurrences were moved 3 -> 4 in this batch; the hardening pins
    # require EVERY /static/ ref in index.html to carry the token.)
    assert INDEX_HTML.count("?v=20261104_r66") == 4
