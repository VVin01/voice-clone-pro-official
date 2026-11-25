"""Batch BH (2026-11-06): the TTS balance line never paints.

Production bug, owner-confirmed on a real phone: #tts-balance-line
sat on the static "tts_balance_loading" placeholder forever —
never the balance, never the BB-4 error fallback — while
GET /api/tts/quota answers in ~0.5 s. Two defects are provable from
the BB loader's code alone:

(1) every paint wrote into the node captured at loader start and
    left its data-i18n="tts_balance_loading" attribute in place,
    so ANY later setLanguage() pass (the profile language sync in
    loadUserProfile, or a manual language toggle) re-stamped the
    static loading string over the painted balance — and nothing
    re-triggered the loader, so the line stayed on "checking…" for
    the rest of the session;
(2) every paint — including the BB error fallback — sat behind
    the fetch await-chain, so a chain that never settles inside
    the Telegram webview paints nothing at all and leaves
    ttsQuotaInFlight latched true, silently blocking every later
    retry (tab re-entries included).

BH makes the stuck state impossible by construction:
BH-1  instant paint from the profile /user/me already carries
      (state.user.tts_balance_seconds — the same field the Voice
      tab paints from), before/while the quota fetch runs;
BH-2  every paint re-queries the node at paint time and strips
      data-i18n + sets dataset.dynamic, so no setLanguage sweep
      can re-stamp the placeholder; setLanguage re-triggers the
      loader for a dynamic line so it repaints in the new language;
BH-3  a 6 s watchdog keyed by a run counter: if the line still
      shows the loading placeholder when it fires, it force-paints
      the tap-to-retry error state and releases the in-flight
      latch; paints from the current run disarm it, stale-run
      paints cannot;
BH-4  nothing else changes; no new i18n keys (the four existing
      balance keys are reused verbatim). Token r80 -> r81.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
APP_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def _between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


def _i18n_values(key: str) -> list[str]:
    out = []
    for line in I18N_JS.splitlines():
        line = line.strip()
        if line.startswith(key + ":"):
            out.append(line.split(":", 1)[1].strip().strip(",").strip('"'))
    return out


# Round 8 (B4 + C2, 2026-11-21) retired #tts-balance-line, paintTtsBalanceLine,
# paintTtsQuotaError, paintTtsQuotaInstant, and all tts_balance_* / voice_tts_balance_line
# keys and helpers while keeping refreshTtsQuota for chip/key/taste/cost-hint updates.
LOADER = _between(APP_JS, "async function refreshTtsQuota", "function updateTtsCharCount")


def test_bh_superseded_by_round8_balance_line_retirement():
    for fn in (
        "paintTtsBalanceLine",
        "paintTtsQuotaError",
        "paintTtsQuotaInstant",
        "formatTtsTabBalanceLine",
        "paintVoiceTtsBalanceLine",
    ):
        assert fn not in APP_JS
    assert 'id="tts-balance-line"' not in INDEX_HTML
    for key in (
        "tts_balance_loading",
        "tts_balance_error",
        "tts_balance_line",
        "tts_balance_line_unlimited",
        "tts_balance_line_admin",
        "tts_balance_line_vip",
        "tts_balance_line_vip_ref",
        "voice_tts_balance_line",
    ):
        assert key not in I18N_JS


def test_bh_refresh_tts_quota_retains_inflight_guard_and_chip_updates():
    assert "if (ttsQuotaInFlight) return;" in LOADER
    assert 'fetchWithTimeout("/api/tts/quota"' in LOADER
    assert "paintTtsChipFromQuota" in LOADER
    assert "paintTtsGeminiKeyState" in LOADER
    assert "paintTtsTasteNote" in LOADER
    assert "paintTtsCostHintTiered" in LOADER


def test_bh2_loader_no_longer_captures_the_node_at_start():
    assert "if (!line || ttsQuotaInFlight) return;" not in APP_JS


def test_bh4_token_bumped_to_r81():
    assert INDEX_HTML.count("20261125_admin_r1") == 5
    assert "20261104_r80" not in INDEX_HTML
