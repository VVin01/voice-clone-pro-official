"""Batch DH (2026-11-10) — Step-2 preview hidden-boot guard + refit on show.

DG's width-first fit ran at boot while the Recap pane was hidden:
parentElement.clientWidth was 0, so it wrote inline width/height 0px
and the preview rendered 2x2 until a window resize. DH: (a) a
non-positive parent width clears the inline size and returns (the CSS
default sizes the frame), (b) the fit is exposed on window, and
(c) showRecapStep re-fits when Step 2 is entered.
"""
import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[2] / "app" / "webapp" / "static" / "js" / "app.js"
INDEX = Path(__file__).resolve().parents[2] / "app" / "webapp" / "static" / "index.html"


def _fit_fn() -> str:
    src = APP_JS.read_text(encoding="utf-8")
    i = src.index("function ddFitPreviewFrame()")
    return src[i:src.index("\n    }", i)]


def test_hidden_parent_width_never_written_as_zero():
    fn = _fit_fn()
    assert "if (parentW <= 0)" in fn
    guard = fn[fn.index("if (parentW <= 0)"):]
    assert 'frame.style.width = "";' in guard
    assert 'frame.style.height = "";' in guard
    # the guard returns before any px write
    assert guard.index("return;") < guard.index("Math.min(300, parentW)")


def test_fit_exposed_for_step_show_refit():
    src = APP_JS.read_text(encoding="utf-8")
    assert "window.__ddFitPreviewFrame = ddFitPreviewFrame;" in src


def test_show_recap_step_refits_on_step2():
    src = APP_JS.read_text(encoding="utf-8")
    i = src.index("function showRecapStep(n)")
    body = src[i:src.index("\nfunction ", i + 10)]
    assert "n === 2" in body
    assert "window.__ddFitPreviewFrame" in body
    assert "requestAnimationFrame" in body


def test_dg_width_first_rule_kept():
    fn = _fit_fn()
    assert "Math.min(300, parentW)" in fn
    assert "window.innerHeight < 640" in fn


def test_cache_token_is_r140():
    html = INDEX.read_text(encoding="utf-8")
    assert html.count("20261104_r140") == 4
    assert "20261104_r129" not in html
