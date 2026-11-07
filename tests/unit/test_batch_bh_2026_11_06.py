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


# The BB-4 extraction span: refreshTtsQuota through the BH helpers.
LOADER = _between(APP_JS, "async function refreshTtsQuota", "function updateTtsCharCount")
PAINT = _between(APP_JS, "function paintTtsBalanceLine", "function paintTtsQuotaError")
ERROR_PAINT = _between(APP_JS, "function paintTtsQuotaError", "function paintTtsQuotaInstant")
INSTANT = _between(APP_JS, "function paintTtsQuotaInstant", "function updateTtsCharCount")


# ── BH-1: instant paint from profile data ───────────────────────

def test_bh1_instant_paint_reads_profile_tts_balance_seconds():
    assert "const profile = state.user;" in INSTANT
    assert 'typeof profile.tts_balance_seconds !== "number"' in INSTANT
    assert "(profile.tts_balance_seconds / 60).toFixed(1)" in INSTANT


def test_bh1_instant_paint_formats_admin_unlimited_else_voice_line():
    # Admins sit outside the daily cap -> unlimited format; everyone
    # else gets the referral-minutes line in the Voice tab's format.
    assert 'tf("tts_balance_line_unlimited", { ref_min: refMin })' in INSTANT
    assert 'tf("voice_tts_balance_line", { min: refMin })' in INSTANT
    assert "profile.is_admin" in INSTANT


def test_bh1_instant_paint_runs_before_the_inflight_gate():
    # The instant paint must land even while a previous (hung) chain
    # holds the latch: it is invoked before the early return.
    call = LOADER.index("paintTtsQuotaInstant();")
    gate = LOADER.index("if (ttsQuotaInFlight) return;")
    assert call < gate


# ── BH-2: fresh-node, de-i18ned paints + setLanguage re-trigger ─

def test_bh2_every_paint_requeries_the_node_and_strips_data_i18n():
    assert 'document.getElementById("tts-balance-line")' in PAINT
    assert 'line.removeAttribute("data-i18n")' in PAINT
    assert 'line.dataset.dynamic = "1"' in PAINT
    # The strip happens BEFORE the mutation writes the new text.
    assert PAINT.index('line.removeAttribute("data-i18n")') < PAINT.index("mutate(line)")


def test_bh2_loader_no_longer_captures_the_node_at_start():
    # The BB shape (captured node + combined early return) is gone.
    assert "if (!line || ttsQuotaInFlight) return;" not in APP_JS


def test_bh2_setlanguage_retriggers_refresh_for_a_dynamic_line():
    setlang = _between(I18N_JS, "function setLanguage(lang)", "window.__langPersistPromise")
    assert 'document.getElementById("tts-balance-line")' in setlang
    assert 'dataset.dynamic === "1"' in setlang
    assert "window.refreshTtsQuota()" in setlang
    # The re-trigger runs AFTER the static [data-i18n] sweep it
    # complements, not before it.
    sweep = setlang.index('querySelectorAll("[data-i18n]")')
    assert sweep < setlang.index("window.refreshTtsQuota()")


# ── BH-3: run-keyed watchdog ────────────────────────────────────

def test_bh3_watchdog_armed_per_run_at_six_seconds():
    assert "const runId = ++ttsQuotaRunCounter;" in LOADER
    assert "ttsQuotaWatchdogTimer = setTimeout(" in LOADER
    assert "}, 6000);" in LOADER
    # A stale run's watchdog callback bails on the run counter.
    assert "if (runId !== ttsQuotaRunCounter) return;" in LOADER


def test_bh3_watchdog_force_paints_error_and_releases_the_latch():
    watchdog = _between(LOADER, "ttsQuotaWatchdogTimer = setTimeout(", "}, 6000);")
    assert 'getAttribute("data-i18n") === "tts_balance_loading"' in watchdog
    assert 'textContent === t("tts_balance_loading")' in watchdog
    assert "paintTtsQuotaError(runId);" in watchdog
    assert "ttsQuotaInFlight = false;" in watchdog


def test_bh3_only_a_current_run_paint_disarms_the_watchdog():
    assert "if (runId === ttsQuotaRunCounter && ttsQuotaWatchdogTimer)" in PAINT
    assert "clearTimeout(ttsQuotaWatchdogTimer);" in PAINT
    # The instant paint is not a fetch run: it paints under key -1
    # and can never disarm a watchdog.
    assert "paintTtsBalanceLine(-1," in INSTANT


def test_bh3_error_paint_is_the_bb_tap_to_retry_state():
    assert 'line.textContent = t("tts_balance_error");' in ERROR_PAINT
    assert 'line.dataset.quotaError = "1";' in ERROR_PAINT
    assert 'line.classList.add("tts-balance-retry");' in ERROR_PAINT


# ── BH-4: scope — reused keys verbatim, token bump only ─────────

def test_bh4_reused_i18n_keys_unchanged_in_both_languages():
    assert _i18n_values("tts_balance_line") == [
        "🎧 မိတ်ဆက်ဆု လက်ကျန် (TTS): {ref_min} မိနစ် · 📅 ဒီနေ့ သုံးနိုင်သေးချိန်: {remain_min} မိနစ်",
        "🎧 Referral balance (TTS): {ref_min} min · 📅 Left today: {remain_min} min",
    ]
    assert _i18n_values("tts_balance_line_unlimited") == [
        "🎧 မိတ်ဆက်ဆု လက်ကျန် (TTS): {ref_min} မိနစ် · 📅 နေ့စဉ် ကန့်သတ်ချက် မရှိပါ",
        "🎧 Referral balance (TTS): {ref_min} min · 📅 No daily limit",
    ]
    assert _i18n_values("voice_tts_balance_line") == [
        "🎧 TTS အသံ အတွက် လက်ကျန်: {min} မိနစ် — TTS Tab တွင် သုံးနိုင်ပါသည်",
        "🎧 TTS audio balance: {min} min — spend it in the TTS tab",
    ]


def test_bh4_token_bumped_to_r81():
    assert INDEX_HTML.count("20261104_r107") == 4
    assert "20261104_r80" not in INDEX_HTML
