"""Batch DG (2026-11-10) — Step-2 9:16 preview sizing.

Owner report (Telegram Desktop, admin account, screenshot): the
preview rendered ~110px wide on a 502x888 screen. Root cause: DD's
ddFitPreviewFrame capped the frame by a percentage of the viewport
HEIGHT (36% at rest, 22% once window.scrollY > 90) — on tall or
desktop webviews that squeezes the frame far below its 300px design
width, too small to read subtitles or place blur boxes, and it
shrank while the user was working.

Fix: width-first sizing (min(300, parent width), height follows the
9:16 aspect); a height guard ONLY on short viewports (<640px, 44%);
the scroll listener/shrink is gone.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")


def _fit_fn() -> str:
    start = APP_JS.index("function ddFitPreviewFrame()")
    end = APP_JS.index("\n    }\n", start)
    return APP_JS[start:end]


def test_fit_is_width_first():
    fn = _fit_fn()
    assert "Math.min(300, parentW)" in fn
    # width is assigned from w directly; height follows the aspect
    assert 'frame.style.width = `${Math.round(w)}px`' in fn
    assert 'frame.style.height = `${Math.round(w * 16 / 9)}px`' in fn


def test_fit_has_no_height_percent_caps_or_scroll_shrink():
    fn = _fit_fn()
    assert "0.36" not in fn
    assert "0.22" not in fn
    assert "scrollY" not in fn
    # and no scroll listener drives the fit anywhere in the file
    assert 'addEventListener("scroll", ddFitPreviewFrame' not in APP_JS


def test_fit_short_viewport_guard_only():
    fn = _fit_fn()
    assert "window.innerHeight < 640" in fn
    assert "0.44" in fn


def test_fit_still_registered_on_resize_and_ratio_change():
    assert 'addEventListener("resize", ddFitPreviewFrame)' in APP_JS
    # non-9:16 ratios still clear the inline size
    fn = _fit_fn()
    assert 'frame.style.width = ""' in fn and 'frame.style.height = ""' in fn


def test_cache_token_is_r139():
    assert INDEX.count("20261104_r139") == 4
