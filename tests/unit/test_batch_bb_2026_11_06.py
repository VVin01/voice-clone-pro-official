"""Batch BB (2026-11-06): the reopening-gate fix batch.

BB-1  TTS-tab Edge synthesis crashed on EVERY job: the service built
      EdgeTTSEngine positionally while the constructor is keyword-only.
      The regression test executes the service's REAL edge branch
      (_synthesize_tts_audio_sync) with only the network boundary
      (generate_channel99_voice, post-construction) stubbed — the
      engine class itself is never mocked.
BB-2  All three speed selectors (Voice / TTS / Recap) become ONE
      liquid segmented capsule (AZ-9 language): sliding gradient pill,
      active segment expanded with value + word, inactive = compact
      value. Values, defaults and state write-paths are unchanged;
      labels are PIL/Padauk-measured against the 360px budget.
BB-3  TTS jobs are labelled 🎧 TTS (sky) in the user History hub and
      every admin list that renders job types.
BB-4  The TTS quota line could sit on "checking…" forever: the loader
      was the only one ignoring fetchWithTimeout — one bare fetch, no
      retry, failures swallowed (diagnosed; both live sightings sat in
      deploy-restart windows). Loader now: timeout + bounded retries +
      in-flight guard + tap-to-retry fallback paint.
BB-5  Delivered TTS files arrived as "VoiceClonePro_TTS_File.<jobid>"
      (no extension): the job id was passed in the helper's ext slot.
BB-6  The Home Support row opens the admin chat INSIDE Telegram
      (openTelegramLink), scoped to openSupportContact only.
BB-8  The prefs-saved toast fired on every Recap step change: the
      delegated saver PUT + toasted byte-identical payloads. Saves
      (and the toast) now fire only when a value actually changed.
"""
from __future__ import annotations

import math
import re
import shutil
import struct
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import tts_service
from app.services.download_names import branded_download_filename
from app.services.ledger import get_minute_balance, get_or_create_user
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")
TTS_SRC = (ROOT / "app/services/tts_service.py").read_text(encoding="utf-8")


def _between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


def _i18n_values(key: str) -> list[str]:
    """Both locale values for a key, in file order (my, en)."""
    return re.findall(rf'{re.escape(key)}:\s*"([^"]*)"', I18N_JS)


# ── Cache token ─────────────────────────────────────────────────────

def test_bb_cache_token_is_r79():
    assert HTML.count("20261115_r153") == 4
    assert "20261104_r74" not in HTML


# ── BB-1: Edge constructor ──────────────────────────────────────────

def test_bb1_edge_construction_is_keyword_only_at_the_call_site():
    assert "EdgeTTSEngine(\n            ffmpeg=ffmpeg, work_dir=work_dir" in TTS_SRC
    assert "EdgeTTSEngine(\n            ffmpeg," not in TTS_SRC


def _write_wav(path: Path, seconds: float = 0.5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        frames = b"".join(
            struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * i / 44100)))
            for i in range(int(44100 * seconds))
        )
        w.writeframes(frames)
    return path


def _edge_job(**over):
    job = SimpleNamespace(
        id="bb1-edge-job",
        input_text="မင်္ဂလာပါ စမ်းသပ်စာသားဖြစ်ပါသည်",
        engine="edge",
        edge_voice="my-MM-ThihaNeural",
        preset_voice="my-MM-ThihaNeural",
        speed_preset="+12%",
    )
    for k, v in over.items():
        setattr(job, k, v)
    return job


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_bb1_edge_branch_runs_the_real_construction(tmp_path, monkeypatch):
    """The REAL EdgeTTSEngine is constructed by the service's own edge
    branch; only generate_channel99_voice (the network boundary, which
    internally calls edge_tts.Communicate) is stubbed AFTER
    construction. Pre-fix this raised TypeError on the spot."""
    from app.services.recap_engine.core import EdgeTTSEngine

    seen: dict = {}

    def fake_generate(self, text, voice):
        seen["engine"] = self
        seen["voice"] = voice
        seen["text"] = text
        return _write_wav(Path(self.work_dir) / "edge_fake.wav")

    monkeypatch.setattr(EdgeTTSEngine, "generate_channel99_voice", fake_generate)
    monkeypatch.setattr(settings, "storage_root", tmp_path)

    out_path, duration = tts_service._synthesize_tts_audio_sync(_edge_job())

    assert isinstance(seen["engine"], EdgeTTSEngine)
    assert seen["engine"].rate == "+12%"
    assert seen["voice"] == "my-MM-ThihaNeural"
    assert out_path.is_file() and out_path.suffix == ".wav"
    assert out_path.stat().st_size >= 256
    assert duration > 0


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_bb1_gemini_branch_twin(tmp_path, monkeypatch):
    """Same seam, Gemini branch: real GeminiTTSEngine construction,
    generate stubbed post-construction, keys stubbed at the collector."""
    from app.services.recap_engine import gemini_tts_engine as gem_mod

    seen: dict = {}

    def fake_generate(self, text, voice):
        seen["engine"] = self
        seen["voice"] = voice
        return _write_wav(Path(self.work_dir) / "gem_fake.wav")

    monkeypatch.setattr(gem_mod.GeminiTTSEngine, "generate_channel99_voice", fake_generate)
    monkeypatch.setattr(gem_mod, "collect_gemini_keys", lambda: ["bb-test-key"])
    monkeypatch.setattr(settings, "storage_root", tmp_path)

    job = _edge_job(id="bb1-gem-job", engine="gemini", edge_voice=None,
                    preset_voice="Charon")
    out_path, duration = tts_service._synthesize_tts_audio_sync(job)

    assert isinstance(seen["engine"], gem_mod.GeminiTTSEngine)
    assert seen["voice"] == "Charon"
    assert out_path.is_file() and duration > 0


# ── BB-5: delivered filename ────────────────────────────────────────

def test_bb5_branded_filename_shape():
    name = branded_download_filename("TTS", "wav", job_id="d07508e7f9d442cf")
    assert name == "VoiceClonePro_TTS_d442cf.wav"
    assert "branded_download_filename(\"TTS\", \"wav\", job_id=job.id)" in TTS_SRC
    assert "branded_download_filename(\"TTS\", job.id)" not in TTS_SRC


@pytest.mark.asyncio
async def test_bb5_delivery_sends_a_branded_wav_name(tmp_path, monkeypatch):
    import aiogram
    import aiogram.types

    sent: dict = {}

    class FakeFSInputFile:
        def __init__(self, path, filename=None):
            self.path = path
            self.filename = filename

    class FakeSession:
        async def close(self):
            pass

    class FakeBot:
        def __init__(self, token):
            self.session = FakeSession()

        async def send_audio(self, chat_id, audio, caption, parse_mode):
            sent["chat_id"] = chat_id
            sent["audio"] = audio
            sent["caption"] = caption

    monkeypatch.setattr(aiogram, "Bot", FakeBot)
    monkeypatch.setattr(aiogram.types, "FSInputFile", FakeFSInputFile)
    monkeypatch.setattr(settings, "bot_token", "bb-test-token")

    out = _write_wav(tmp_path / "bb5.wav")
    job = SimpleNamespace(id="d07508e7f9d442cf", chat_id=4242, user_id=4242)
    await tts_service._deliver_tts_to_chat(job, out)

    name = sent["audio"].filename
    assert name.startswith("VoiceClonePro_TTS_")
    assert name.endswith(".wav")
    assert sent["audio"].path == str(out)
    assert sent["chat_id"] == 4242


# ── BB-2: speed capsules ────────────────────────────────────────────

def _capsule(html_id: str) -> str:
    m = re.search(
        rf'<div class="speed-capsule" id="{html_id}">(.*?)</div>', HTML, re.S
    )
    assert m, f"capsule missing: {html_id}"
    return m.group(1)


def test_bb2_all_three_selectors_are_capsules():
    for cid in ("speed-selector", "tts-speed-picker", "recap-speed-picker"):
        block = _capsule(cid)
        assert '<span class="speed-capsule-pill"' in block
        assert 'class="pill-btn"' not in block


def test_bb2_voice_capsule_values_defaults_and_labels():
    block = _capsule("speed-selector")
    assert re.findall(r'data-speed="([^"]+)"', block) == ["-6%", "+0%", "+12%", "+20%", "+25%"]
    assert '<button class="speed-seg active" data-speed="+12%">' in block
    for key in ("speed_m6", "speed_0", "speed_12_capsule", "speed_20", "speed_25"):
        assert f'data-i18n="{key}"' in block
    for short in ("-6%", "+0%", "+12%", "+20%", "+25%"):
        assert f'<span class="seg-short">{short}</span>' in block


def test_bb2_five_option_capsules_values_defaults_and_labels():
    for cid, attr in (("speed-selector", "data-speed"),
                      ("tts-speed-picker", "data-tts-speed"),
                      ("recap-speed-picker", "data-speed")):
        block = _capsule(cid)
        assert re.findall(rf'{attr}="([^"]+)"', block) == ["-6%", "+0%", "+12%", "+20%", "+25%"]
        assert f'<button type="button" class="speed-seg active" {attr}="+12%">' in block \
            or f'<button class="speed-seg active" {attr}="+12%">' in block
        for key in ("speed_m6", "speed_0", "speed_12_capsule", "speed_20", "speed_25"):
            assert f'data-i18n="{key}"' in block, (cid, key)
        for short in ("-6%", "+0%", "+12%", "+20%", "+25%"):
            assert f'<span class="seg-short">{short}</span>' in block


def test_bb2_css_component():
    bb = CSS[CSS.index("BATCH BB"):]
    assert "linear-gradient(90deg, #0a84ff, #38bdf8)" in bb
    assert re.search(r"\.speed-seg\s*\{[^}]*min-height: 44px", bb)
    assert re.search(r"\.speed-seg\.active\s*\{[^}]*flex-grow: 2\.4", bb)
    assert re.search(r"\.speed-seg:active\s*\{[^}]*scale\(0\.97\)", bb)
    assert ".speed-seg.active .seg-full" in bb
    assert ".speed-seg.active .seg-short" in bb
    # Glass track + the retired recap grid superseded by the flex capsule.
    assert re.search(r"\.speed-capsule\s*\{[^}]*var\(--glass-2-bg\)", bb)
    assert "#recap-speed-picker.speed-capsule { display: flex; }" in bb
    # Stacked speed rows + title inset (first-glyph clip fix).
    assert ".control-row.speed-row" in bb
    assert "#recap-preview-speed-group .card-label" in bb
    # Fallback nets + reduced motion learned the new surface.
    assert CSS.count(".speed-capsule") >= 6


def test_bb2_js_wiring_and_state_paths_unchanged():
    for sel in ('"#speed-selector .speed-seg"', '"#tts-speed-picker .speed-seg"',
                '"#recap-speed-picker .speed-seg"'):
        assert sel in APP_JS, sel
    assert '"#speed-selector .pill-btn"' not in APP_JS
    assert '"#tts-speed-picker .pill-btn"' not in APP_JS
    assert '"#recap-speed-picker .pill-btn"' not in APP_JS
    # The state write paths are byte-identical to the pill era.
    assert 'state.speed = pill.getAttribute("data-speed") || "+12%";' in APP_JS
    assert 'ttsState.speed = btn.getAttribute("data-tts-speed") || "+12%";' in APP_JS
    assert 'state.recapSpeed = btn.getAttribute("data-speed") || "+12%";' in APP_JS
    # The mover exists and is re-run on tab show / resize / language.
    assert "function moveSpeedPill(capsule)" in APP_JS
    assert "function moveSpeedPills()" in APP_JS
    assert APP_JS.count("moveSpeedPills()") >= 5


def test_bb2_labels_fit_the_360px_capsule_budget():
    """PIL/Padauk at the component's 13px: every active (value + word)
    label fits the active segment's content box at a 360px viewport,
    and every compact value fits an inactive segment. Geometry:
    strip = 360 − 2×16 (viewport) − 2×16 (card) − 2×6 (capsule
    padding) − 4×4 gaps = 268px (5-seg) / 276px (3-seg); active
    flex-grow 2.4; segment padding 3px/side; 2px safety margin."""
    from PIL import ImageFont

    font = ImageFont.truetype(str(ROOT / "app/webapp/static/fonts/Padauk-Bold.ttf"), 13)
    active5 = 268 * 2.4 / 6.4 - 6 - 2
    inactive5 = 268 / 6.4 - 6 - 2
    for key in ("speed_m6", "speed_0", "speed_12_capsule", "speed_20", "speed_25"):
        for value in _i18n_values(key):
            assert font.getlength(value) <= active5, (key, value)
    for short in ("-6%", "+0%", "+12%", "+20%", "+25%"):
        assert font.getlength(short) <= inactive5, short
    # The reason speed_12_capsule exists: the stock EN label cannot fit.
    assert font.getlength(_i18n_values("speed_12")[1]) > active5
    assert _i18n_values("speed_12_capsule")[0] == _i18n_values("speed_12")[0]


# ── BB-3: TTS labelling ─────────────────────────────────────────────

def test_bb3_tts_badge_key_in_both_locales():
    assert _i18n_values("job_badge_tts") == ["🎧 TTS", "🎧 TTS"]


def test_bb3_history_and_admin_surfaces_label_tts():
    hub = _between(APP_JS, "function hubHistoryRowHtml", "function renderHistoryHub")
    assert 'j.job_type === "TTS"' in hub
    assert '"🎧"' in hub and 't("job_badge_tts")' in hub
    # Admin: user-detail jobs, history list, media modal, queue tag.
    assert APP_JS.count("badge-tts") >= 3
    assert APP_JS.count('job_badge_tts') >= 3
    assert re.search(r"\.history-item-badge\.badge-tts\s*\{[^}]*var\(--sky-soft\)", CSS)
    assert re.search(r"\.hub-type-icon\.tts\s*\{[^}]*var\(--sky-soft\)", CSS)
    # Voice/recap rendering untouched.
    assert "'badge-recap' : 'badge-voice'" in APP_JS or "badge-recap" in APP_JS


# ── BB-4: quota loader resilience + endpoint ────────────────────────

def test_bb4_loader_uses_timeout_retries_and_a_fallback_paint():
    body = _between(APP_JS, "async function refreshTtsQuota", "function updateTtsCharCount")
    assert 'fetchWithTimeout("/api/tts/quota"' in body
    assert 'await fetch("/api/tts/quota"' not in APP_JS
    assert "retryDelaysMs" in body and "1800" in body
    assert "ttsQuotaInFlight" in body
    assert 't("tts_balance_error")' in body
    assert 'line.dataset.quotaError = "1"' in body
    # The success paints are unchanged from AW.
    # SUPERSEDED by Polish Pass 1 (2026-11-14, brief 1D): refreshTtsQuota
    # now delegates to tier-truthful formatTtsTabBalanceLine (Admin sees no referral line).
    assert "formatTtsTabBalanceLine" in body
    assert 'formatTtsTabBalanceLine' in APP_JS
    assert 'tf("tts_balance_line", {' in APP_JS
    # Tap-to-retry is wired in initTtsTab.
    init = _between(APP_JS, "function initTtsTab", "document.addEventListener")
    assert 'balLine.dataset.quotaError === "1"' in init
    assert _i18n_values("tts_balance_error") == [
        "လက်ကျန် မရသေးပါ — ပြန်စမ်းရန် တို့ပါ",
        "Balance unavailable — tap to retry",
    ]
    assert ".tts-balance-retry" in CSS


@pytest.mark.asyncio
async def test_bb4_quota_endpoint_answers_the_tabs_request(db_session):
    """Handler-level: GET /api/tts/quota's own function under the
    tab's request (a FREE user, then with referral seconds banked)."""
    user, _ = await get_or_create_user(db_session, 987650001)
    out = await api_mod.get_tts_quota(user=user, db=db_session)
    assert out["tier"] == "FREE"
    assert out["daily_limit_minutes"] == settings.tts_daily_limit_minutes_free
    assert out["remaining_seconds"] == settings.tts_daily_limit_minutes_free * 60
    assert out["referral_seconds"] == 0
    assert out["unlimited"] is False

    bal = await get_minute_balance(db_session, user.id)
    bal.referral_seconds = 300
    await db_session.flush()
    out = await api_mod.get_tts_quota(user=user, db=db_session)
    assert out["referral_seconds"] == 300
    assert out["referral_minutes"] == 5.0


# ── BB-6: support opens the admin chat in Telegram ──────────────────

def test_bb6_support_uses_open_telegram_link_scoped():
    body = _between(APP_JS, "function openSupportContact", "window.openSupportContact")
    assert "openTelegramLink" in body
    assert "_supportLink" in body
    assert "openLink" in body  # fallback preserved
    # The shared tutorial opener is NOT switched.
    tut = _between(APP_JS, "function openTutorialUrl", "// ── Refer & Support")
    assert "openTelegramLink" not in tut
    assert "openLink" in tut
    # The Home row still calls the same handler (AY/round-6 pins hold).
    assert 'onclick="openSupportContact()"' in HTML


# ── BB-8: prefs toast only on real change ───────────────────────────

def test_bb8_save_is_change_gated_and_seeded_on_apply():
    body = _between(APP_JS, "async function saveAccountPrefsNow", "function saveAccountPrefsSoon")
    assert "userPrefsLastSavedJson" in body
    assert "prefsJson === userPrefsLastSavedJson" in body
    # The PUT + toast happen only after the gate, on success.
    assert body.index("prefsJson === userPrefsLastSavedJson") < body.index('fetch("/api/user/preferences"')
    assert body.index("userPrefsLastSavedJson = prefsJson;") < body.index("showPrefsSavedNote();")
    # applyAccountPrefs seeds the snapshot after the control resync.
    apply_body = _between(APP_JS, "function applyAccountPrefs", "async function loadAccountPrefs")
    assert "userPrefsLastSavedJson = prefsSnapshotJson(collectAccountPrefs());" in apply_body
    assert apply_body.index("resyncRecapControlsAfterPrefs") < apply_body.index("userPrefsLastSavedJson")
    # The toast itself is untouched (AZ pin holds).
    note = _between(APP_JS, "function showPrefsSavedNote", "async function saveAccountPrefsNow")
    assert 'showStudioToast(t("live_prefs_saved"), "success", 2200);' in note


# ── i18n: every BB key complete in both locales ─────────────────────

def test_bb_new_i18n_keys_complete_in_both_locales():
    for key in ("tts_balance_error", "job_badge_tts", "speed_12_capsule"):
        values = _i18n_values(key)
        assert len(values) == 2 and all(values), key
