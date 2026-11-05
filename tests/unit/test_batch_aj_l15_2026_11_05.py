"""Batch AJ / L15 — Dockerfile pip line + Chromium-renderer signal.

* Dockerfile installed ``-r requirements.txt gradio_client`` — the
  trailing UNPINNED name is redundant (gradio_client==2.7.2 is
  pinned in requirements.txt) and a future footgun.
* text_overlay_service's Chromium renderer is silently dead in prod
  (playwright is not installed / no Chromium in the image), so every
  title falls back to Pillow with no signal. Requesting the Chromium
  path while it is unavailable must now emit a ONE-TIME warning log.
  (Playwright is deliberately NOT added to requirements — there is
  no Chromium in the image, so the package alone would change
  nothing.)
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.services.recap_engine.services import text_overlay_service as tos

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_pip_install_has_no_unpinned_trailing_package():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    pip_lines = [
        line.strip() for line in dockerfile.splitlines()
        if "pip install" in line and "requirements.txt" in line
    ]
    assert pip_lines, "requirements install line not found"
    for line in pip_lines:
        assert line == "RUN pip install --no-cache-dir -r requirements.txt"
    # The pin itself stays in requirements.txt.
    req = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "gradio_client==2.7.2" in req


def test_chromium_request_without_playwright_warns_once(tmp_path, caplog):
    if tos.sync_playwright is not None:
        pytest.skip("playwright is installed in this environment")
    tos._CHROMIUM_UNAVAILABLE_WARNED = False
    font = tmp_path / "font.ttf"
    font.write_bytes(b"fake-font")
    kwargs = dict(
        text="title", out_path=tmp_path / "out.png", width=400, height=120,
        font_path=font, font_size=40, min_font_size=20, max_lines=2,
        single_line=False, bg_alpha=0.5, radius=10, padding_x=10,
        padding_y=8, stroke_px=1.0,
    )
    with caplog.at_level(logging.WARNING, logger=tos.__name__):
        with pytest.raises(RuntimeError):
            tos._render_browser_text_png(**kwargs)
        with pytest.raises(RuntimeError):
            tos._render_browser_text_png(**kwargs)
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Chromium" in r.getMessage()
    ]
    assert len(warnings) == 1, "exactly one warning for repeated requests"
    assert "Pillow" in warnings[0].getMessage()
