"""Mini App ENTRY GATE LOCK tests (feat-miniapp-entry-gate-2026-10-30).

The server gate is unchanged (writes live-checked, fail-closed). These
tests pin the new frontend lock that closes the last gap: the Mini App
used to OPEN fully for non-members (banner only) and stayed visually
open for a join-then-leave user until a write failed. Now:

(a) the page ships locked from first paint (body.gate-locked + the
    #gate-lock overlay markup with per-chat chips, both join buttons
    and the re-check button);
(b) startup runs a LIVE POST /api/user/verify-membership check — the
    unlock path is gated on verified=true, never on cached /me flags;
(c) the check re-runs on visibility/focus and on an interval, so a
    member who leaves either chat is locked again;
(d) GATE_NOT_CONFIGURED is a distinct lock mode (contact admin), not
    an unlock;
(e) admins are never locked.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"


def _html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    return (STATIC / "js" / "app.js").read_text(encoding="utf-8")


def _i18n() -> str:
    return (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")


def _css() -> str:
    return (STATIC / "css" / "style.css").read_text(encoding="utf-8")


def test_lock_overlay_markup_exists_and_page_starts_locked():
    html = _html()
    # Page loads locked: the body carries the lock class from first paint.
    assert 'class="dark-theme gate-locked"' in html
    # Full-screen lock overlay with its own chips, both join links and
    # the re-check button (distinct ids from the banner's).
    assert 'id="gate-lock"' in html
    assert 'id="gate-lock-chip-channel"' in html
    assert 'id="gate-lock-chip-group"' in html
    assert 'id="gate-lock-link-channel"' in html
    assert 'id="gate-lock-link-group"' in html
    assert 'id="gate-lock-btn-verify"' in html
    assert 'onclick="verifyEntryGate()"' in html
    assert 'id="gate-lock-status"' in html


def test_lock_css_covers_screen_and_blocks_app():
    css = _css()
    assert ".gate-lock {" in css
    assert "body.gate-locked #app" in css  # app behind the lock is inert
    assert "pointer-events: none" in css


def test_startup_calls_live_verify_membership():
    js = _js()
    # Startup wires the entry gate...
    assert "initEntryGate();" in js
    assert 'runEntryGateCheck("startup")' in js
    # ...and the check itself is the LIVE endpoint, not /api/user/me.
    assert 'fetch("/api/user/verify-membership", { method: "POST", headers: getAuthHeaders() })' in js


def test_unlock_only_on_verified_true():
    js = _js()
    # The single unlock decision point: verified=true (or admin via /me).
    assert "if (data.verified === true || (state.user && state.user.is_admin))" in js
    assert "unlockEntryGate();" in js
    # A non-verdict (network error) must NOT unlock: fail-closed catch.
    assert "No live verdict -> no unlock (fail-closed)" in js


def test_recheck_on_visibility_focus_and_interval():
    js = _js()
    assert 'document.addEventListener("visibilitychange"' in js
    assert 'runEntryGateCheck("visibility")' in js
    assert 'window.addEventListener("focus"' in js
    assert 'runEntryGateCheck("interval")' in js
    assert "}, 60000);" in js
    # A gated-write 403 also re-runs the live check (re-lock path).
    assert 'runEntryGateCheck("gate-403")' in js


def test_gate_not_configured_is_distinct_lock_mode():
    js = _js()
    assert 'data.gate_configured === false' in js
    assert 'setEntryGateMode("not-configured", data)' in js
    # The not-configured mode reuses the distinct contact-admin copy.
    assert "gate_not_configured_title" in js
    assert "gate_not_configured_desc" in js


def test_admin_force_unlock_from_profile():
    js = _js()
    assert "if (data.is_admin) unlockEntryGate();" in js


def test_lock_chips_share_banner_chip_semantics():
    js = _js()
    # renderGateChips paints BOTH surfaces (banner + lock) from one call.
    assert 'paintGateChip(document.getElementById("gate-lock-chip-channel"), "📢 Channel", channelOk)' in js
    assert 'paintGateChip(document.getElementById("gate-lock-chip-group"), "👥 Group", groupOk)' in js
    # ...and the verdict path repaints them from the live response.
    assert "renderGateChips(data.channel_verified, data.group_verified)" in js


def test_lock_i18n_keys_exist_in_both_languages():
    i18n = _i18n()
    # my + en blocks both define the lock copy.
    assert i18n.count("gate_lock_title:") == 2
    assert i18n.count("gate_lock_desc:") == 2
    html = _html()
    assert 'data-i18n="gate_lock_title"' in html
    assert 'data-i18n="gate_lock_desc"' in html
