"""Batch CZ (2026-11-09): speed-capsule invisible active segment.

Found by Ulfa 2026-11-09 (screenshot + "the middle button only appears
when tapped"): in the TTS tab's speed selector the middle (+12%) segment
was a wide white blank until a segment was tapped. Live reproduction
(evidence: ~/workspace/tg-shoot/capsule-diag/, findings.json +
screenshots 01/01b/02/03/04/05/08) root-caused it deterministically:

- The speed capsule (Batch BB-2) paints ONE gradient pill behind the
  .active segment; the active segment's text is WHITE (#ffffff) and is
  only readable on that blue pill. The pill starts at opacity 0 and
  only appears when moveSpeedPill() measures the active segment and
  adds .on.
- setupTabs()'s dock (.nav-item) click handler duplicates switchTab's
  pane switching but NEVER called moveSpeedPills()/moveSegPills()
  (switchTab does). Every other paint trigger fires while the target
  panes are display:none -> moveSpeedPill early-returns on
  !active.offsetWidth -> the pill is never positioned, .on never added.
- The hardcoded .active segment (white text, flex-grow 2.4 — the
  WIDEST segment) then sits white-on-white over the near-white glass
  capsule: the wide blank Ulfa saw. Zero pageerrors in the repro
  session; entering TTS via a switchTab() path (header chip) painted
  correctly; a segment tap healed it (click handlers call
  moveSpeedPill directly).

Fix: the dock handler now mirrors switchTab — moveSpeedPills() +
moveSegPills() right after the pane switch, plus a requestAnimationFrame
repeat of both as a webview-layout safety net (idempotent, cheap).
The Recap-pane first-show path is covered the same way: the dock
handler repaints on tab-recap entry, and setRecapPreviewState's rAF
door (Batch BB-2) repaints when a restored preview becomes ready.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")


def _dock_handler() -> str:
    """The setupTabs() bottom-nav click handler body."""
    return APP_JS.split("function setupTabs()", 1)[1].split(
        "async function loadUserProfile", 1
    )[0]


def _switch_tab_body() -> str:
    return APP_JS.split("function switchTab(tabId)", 1)[1].split(
        "window.switchTab = switchTab;", 1
    )[0]


def _recap_preview_state_body() -> str:
    return APP_JS.split("function setRecapPreviewState(mode)", 1)[1].split(
        "window.setRecapPreviewState = setRecapPreviewState;", 1
    )[0]


# ── Release pin: cache token ─────────────────────────────────────────

def test_cz_cache_token_is_r139():
    assert INDEX_HTML.count("20261104_r139") == 4
    assert "20261104_r" + "122" not in INDEX_HTML


# ── CZ: the dock handler must repaint the capsule pills ─────────────

def test_setup_tabs_handler_paints_speed_pills():
    handler = _dock_handler()
    # The handler still does its own pane switching (no delegation).
    assert 'activePane.classList.add("active");' in handler
    # ... and now mirrors switchTab's pill repaint.
    assert "moveSpeedPills();" in handler
    assert "moveSegPills();" in handler


def test_setup_tabs_handler_pill_repaint_follows_pane_switch():
    # The repaint must run AFTER the pane becomes visible — measuring
    # while the pane is display:none is the exact failure this batch
    # fixes (moveSpeedPill early-returns on !active.offsetWidth).
    handler = _dock_handler()
    shown = handler.index('activePane.classList.add("active");')
    assert handler.index("moveSpeedPills();") > shown
    assert handler.index("moveSegPills();") > shown


def test_setup_tabs_handler_has_raf_repaint_safety_net():
    # A requestAnimationFrame repeat of both movers, so a webview whose
    # layout has not settled on the synchronous call still paints the
    # pill on the next frame (idempotent, cheap).
    handler = _dock_handler()
    raf = handler.index("requestAnimationFrame(")
    tail = handler[raf:]
    assert "moveSpeedPills();" in tail
    assert "moveSegPills();" in tail


def test_switch_tab_still_paints_pills():
    # switchTab was the one correct entry path — keep it that way.
    body = _switch_tab_body()
    assert "moveSpeedPills();" in body
    assert "moveSegPills();" in body


def test_recap_preview_state_still_repaints():
    # The residual-risk door for the recap speed picker: when a
    # restored-ready preview unhides its speed group, the rAF repaint
    # must still fire (the dock-handler fix covers tab-recap entry;
    # this door covers the preview becoming ready).
    body = _recap_preview_state_body()
    assert "requestAnimationFrame(" in body
    assert "moveSpeedPills();" in body
    assert "moveSegPills();" in body
