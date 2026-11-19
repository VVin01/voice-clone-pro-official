"""Unified Speed System rebuild tests (feat/speed-system-rebuild).

Verifies:
1. One canonical 5-option speed contract across Voice Clone, TTS, Recap, Prefs,
   and Worker/Estimator layers: ``("-6%", "+0%", "+12%", "+20%", "+25%")``.
2. Identical delivered tempo across VoxCPM, Edge TTS, Curated Persona fallback,
   Gemini TTS, and Recap EdgeSmartSync (including true 1.00x at ``+0%``).
3. Seamless backward compatibility & DB/localStorage/preset migration for legacy
   word presets (``slow``/``normal``/``fast``) and retired percents (``+8%``,
   ``+18%``, ``+22%``).
4. Cross-surface estimation & worker truncation-threshold alignment.
5. Frontend HTML/JS/i18n parity & 360px Padauk-Bold pixel-width fit.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import ImageFont
from pydantic import ValidationError

from app.services.speed import (
    DEFAULT_SPEED,
    DEFAULT_TEMPO,
    MAX_TEMPO,
    MIN_TEMPO,
    SPEED_OPTIONS,
    SPEED_RATES,
    migrate_speed_pref,
    normalize_api_speed,
    resolve_speed,
    resolve_speed_rate,
    resolve_speed_tempo,
)

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Single source of truth & UI/API/Pref surface parity
# ---------------------------------------------------------------------------

def test_canonical_speed_options_and_table():
    assert SPEED_OPTIONS == ("-6%", "+0%", "+12%", "+20%", "+25%")
    assert DEFAULT_SPEED == "+12%"
    assert DEFAULT_TEMPO == 1.12
    assert SPEED_RATES == {"slow": "-6%", "normal": "+12%", "fast": "+20%"}

    expected = [
        ("-6%", -6, 0.94),
        ("+0%", 0, 1.00),
        ("+12%", 12, 1.12),
        ("+20%", 20, 1.20),
        ("+25%", 25, 1.25),
    ]
    for opt, pct, tempo in expected:
        spec = resolve_speed(opt)
        assert spec.rate == opt
        assert spec.pct == pct
        assert spec.tempo == pytest.approx(tempo, abs=1e-6)
        assert MIN_TEMPO <= spec.tempo <= MAX_TEMPO


@pytest.mark.parametrize(
    "legacy,canonical,tempo",
    [
        ("slow", "-6%", 0.94),
        ("0.8", "-6%", 0.94),
        ("0.8x", "-6%", 0.94),
        ("normal", "+12%", 1.12),
        ("fast", "+20%", 1.20),
        ("1.2", "+20%", 1.20),
        ("1.2x", "+20%", 1.20),
        ("+0%", "+0%", 1.00),
        ("0%", "+0%", 1.00),
    ],
)
def test_legacy_presets_resolve_to_exact_canonical_tempo(legacy, canonical, tempo):
    assert resolve_speed_rate(legacy) == canonical
    assert resolve_speed_tempo(legacy) == pytest.approx(tempo, abs=1e-6)
    assert resolve_speed_tempo(legacy) == resolve_speed_tempo(canonical)


def test_all_three_frontend_capsules_identical_five_options():
    for cid, attr in (
        ("speed-selector", "data-speed"),
        ("tts-speed-picker", "data-tts-speed"),
        ("recap-speed-picker", "data-speed"),
    ):
        m = re.search(rf'id="{cid}"[\s\S]*?</div>', INDEX_HTML)
        assert m, f"Missing capsule #{cid}"
        block = m.group(0)
        assert re.findall(rf'{attr}="([^"]+)"', block) == list(SPEED_OPTIONS)
        assert (
            f'<button class="speed-seg active" {attr}="+12%">' in block
            or f'<button type="button" class="speed-seg active" {attr}="+12%">' in block
        )
        assert re.findall(r'data-i18n="([^"]+)"', block) == [
            "speed_m6",
            "speed_0",
            "speed_12_capsule",
            "speed_20",
            "speed_25",
        ]
        assert re.findall(r'<span class="seg-short">([^<]+)</span>', block) == list(SPEED_OPTIONS)


def test_api_prefs_tts_and_recap_share_unified_defaults():
    from app.services.downloader import MediaDownloader
    from app.services.recap_engine.core import EdgeSmartSync
    from app.services.tts_service import TTS_DEFAULT_SPEED, TTS_SPEED_RATES
    from app.webapp import api as api_mod
    import inspect

    assert tuple(api_mod.USER_PREF_ENUMS["voice_speed"]) == SPEED_OPTIONS
    assert tuple(api_mod.USER_PREF_ENUMS["recapSpeed"]) == SPEED_OPTIONS
    assert api_mod.DEFAULT_USER_PREFS["voice_speed"] == DEFAULT_SPEED
    assert api_mod.DEFAULT_USER_PREFS["recapSpeed"] == DEFAULT_SPEED
    assert TTS_SPEED_RATES == SPEED_OPTIONS
    assert TTS_DEFAULT_SPEED == DEFAULT_SPEED
    assert api_mod.TTSGenerateRequest().speed == DEFAULT_SPEED
    assert api_mod.RecapConfigRequest(url="https://example.com/v.mp4").voice_speed == DEFAULT_SPEED
    assert api_mod.VoiceGenerateRequest(text="hello").speed == DEFAULT_SPEED
    assert inspect.signature(MediaDownloader.generate_recap_video).parameters["voice_speed"].default == DEFAULT_SPEED
    assert inspect.signature(EdgeSmartSync.__init__).parameters["edge_tts_rate"].default == DEFAULT_SPEED


def test_bot_locale_choose_speed_reflects_true_rates():
    for lang in ("en", "my"):
        data = json.loads((ROOT / f"locales/{lang}/messages.json").read_text(encoding="utf-8"))
        msg = data["tts_choose_speed"]
        assert "-6%" in msg and "+12%" in msg and "+20%" in msg
        assert "-15%" not in msg and "+15%" not in msg


# ---------------------------------------------------------------------------
# 2. Engine tempo equivalence & explicit +0% truthfulness
# ---------------------------------------------------------------------------

def test_curated_persona_rate_ignores_hidden_offsets_and_honors_exact_speed():
    from app.services.curated_voices import CURATED_VOICES, compute_curated_rate

    # CURATED_VOICES metadata retains its legacy rate_offset field for backwards fixtures
    assert "komin" in CURATED_VOICES
    assert "rate_offset" in CURATED_VOICES["komin"]

    for opt in SPEED_OPTIONS:
        assert compute_curated_rate(12, opt) == opt
        assert compute_curated_rate(-15, opt) == opt
        assert compute_curated_rate(20, opt) == opt
    assert compute_curated_rate(12, "slow") == "-6%"
    assert compute_curated_rate(12, "normal") == "+12%"
    assert compute_curated_rate(12, "fast") == "+20%"


@pytest.mark.asyncio
async def test_generate_edge_tts_uses_unified_rate(tmp_path):
    from app.services.audio import generate_edge_tts

    captured = []

    class _FakeComm:
        def __init__(self, text, voice, rate="+0%"):
            captured.append(rate)

        async def save(self, out_file):
            Path(out_file).write_bytes(b"FAKE_MP3")

    with patch("app.services.audio.edge_tts.Communicate", side_effect=_FakeComm):
        for inp, exp in [
            ("-6%", "-6%"),
            ("+0%", "+0%"),
            ("+12%", "+12%"),
            ("+20%", "+20%"),
            ("+25%", "+25%"),
            ("slow", "-6%"),
            ("normal", "+12%"),
            ("fast", "+20%"),
        ]:
            out = tmp_path / f"out_{exp.replace('%', 'pct')}.mp3"
            await generate_edge_tts("hello", out, speed=inp)
            assert captured[-1] == exp


def test_voxcpm_gemini_and_edge_tts_engine_deliver_identical_tempo(tmp_path):
    from app.services.recap_engine.core import EdgeSmartSync, EdgeTTSEngine
    from app.services.recap_engine.gemini_tts_engine import GeminiTTSEngine

    # EdgeTTSEngine keeps explicit +0% default when unspecified (low-level adapter)
    edge_default = EdgeTTSEngine(ffmpeg="ffmpeg", work_dir=tmp_path / "e0")
    assert edge_default.rate == "+0%"

    # Direct GeminiTTSEngine keeps +0% default for key health probes
    gem_default = GeminiTTSEngine(ffmpeg="ffmpeg", work_dir=tmp_path / "g0", keys=["test-key"])
    assert gem_default.rate == 0

    for opt, pct, _tempo in [
        ("-6%", -6, 0.94),
        ("+0%", 0, 1.00),
        ("+12%", 12, 1.12),
        ("+20%", 20, 1.20),
        ("+25%", 25, 1.25),
        ("slow", -6, 0.94),
        ("normal", 12, 1.12),
        ("fast", 20, 1.20),
    ]:
        e = EdgeTTSEngine(ffmpeg="ffmpeg", work_dir=tmp_path / f"e_{pct}", rate=opt)
        g = GeminiTTSEngine(ffmpeg="ffmpeg", work_dir=tmp_path / f"g_{pct}", rate=opt, keys=["test-key"])
        s = EdgeSmartSync(
            job_dir=str(tmp_path / f"s_{pct}"),
            voice="my-MM-ThihaNeural",
            ffmpeg_path="ffmpeg",
            ffprobe_path="ffprobe",
            edge_tts_rate=opt,
        )
        assert e.rate == resolve_speed_rate(opt)
        assert g.rate == pct
        assert s.edge_tts_rate == resolve_speed_rate(opt)
        assert resolve_speed_tempo(s.edge_tts_rate) == pytest.approx(_tempo, abs=1e-6)


def test_edge_smart_sync_does_not_silently_override_explicit_plus_zero():
    """Guardrail: recap '+0%' must deliver true 1.00x (NOT silent +12%)."""
    from app.services.recap_engine import core as core_mod
    import inspect

    src = inspect.getsource(core_mod.EdgeSmartSync.run)
    assert 'if not effective_rate or effective_rate in ("+0%", "0%", "+0Hz")' not in src
    assert "resolve_speed_tempo(effective_rate)" in src
    assert "resolve_speed_rate(self.edge_tts_rate)" in src
    assert resolve_speed_tempo(resolve_speed_rate("+0%")) == 1.0


# ---------------------------------------------------------------------------
# 3. Preferences migration & API validation
# ---------------------------------------------------------------------------

def test_sanitize_user_prefs_migrates_legacy_values_and_presets():
    from app.webapp.api import _sanitize_user_prefs

    legacy_raw = {
        "voice_speed": "slow",
        "recapSpeed": "+22%",
        "recap_presets": [
            {"name": "P1", "settings": {"recapSpeed": "+18%"}},
            {"name": "P2", "settings": {"recapSpeed": "+8%"}},
        ],
    }
    clean = _sanitize_user_prefs(legacy_raw)
    assert clean["voice_speed"] == "-6%"
    assert clean["recapSpeed"] == "+25%"
    presets = json.loads(clean["recap_presets"])
    assert [p["settings"]["recapSpeed"] for p in presets] == ["+20%", "+12%"]

    # Already-serialized DB JSON string also migrates cleanly on GET
    clean2 = _sanitize_user_prefs({
        "voice_speed": "fast",
        "recapSpeed": "normal",
        "recap_presets": json.dumps([{"name": "Old", "settings": {"recapSpeed": "+22%"}}]),
    })
    assert clean2["voice_speed"] == "+20%"
    assert clean2["recapSpeed"] == "+12%"
    assert json.loads(clean2["recap_presets"])[0]["settings"]["recapSpeed"] == "+25%"

    # Garbage is rejected/dropped safely
    bad = _sanitize_user_prefs({"voice_speed": "bogus", "recapSpeed": "+99%"})
    assert "voice_speed" not in bad
    assert "recapSpeed" not in bad


def test_api_models_accept_canonical_and_legacy_reject_bogus():
    from app.webapp.api import RecapConfigRequest, VoiceGenerateRequest

    for val, exp in [
        ("-6%", "-6%"), ("+0%", "+0%"), ("+12%", "+12%"), ("+20%", "+20%"), ("+25%", "+25%"),
        ("slow", "-6%"), ("normal", "+12%"), ("fast", "+20%"),
        ("+8%", "+8%"), ("+18%", "+18%"), ("+22%", "+22%"),
    ]:
        assert VoiceGenerateRequest(text="hi", speed=val).speed == exp
        assert RecapConfigRequest(url="https://example.com/v", voice_speed=val).voice_speed == exp

    with pytest.raises(ValidationError):
        VoiceGenerateRequest(text="hi", speed="+99%")
    with pytest.raises(ValidationError):
        RecapConfigRequest(url="https://example.com/v", voice_speed="warp9")


@pytest.mark.asyncio
async def test_create_voice_clone_job_normalizes_speed_preset(db_session, tmp_path, monkeypatch):
    from app.models.schema import VoiceModel
    from app.services import queue as queue_mod
    from app.services.ledger import get_or_create_user
    from app.services.queue import create_voice_clone_job

    user, _ = await get_or_create_user(db_session, telegram_id=991001)
    user2, _ = await get_or_create_user(db_session, telegram_id=991002)
    model = VoiceModel(
        name="SpdNormModel", slug="voxcpm-cloud-ai", type="SYSTEM",
        model_dir=str(tmp_path), pth_path="voxcpm", status="ACTIVE",
        is_default_free=True, allowed_for_free=True, allowed_for_purchased=True,
    )
    db_session.add(model)
    await db_session.flush()

    monkeypatch.setattr(queue_mod, "grant_monthly_free_allowance", AsyncMock(return_value=None))
    from app.services.ledger import get_minute_balance
    b1 = await get_minute_balance(db_session, user.id)
    b1.purchased_seconds = 600
    b2 = await get_minute_balance(db_session, user2.id)
    b2.purchased_seconds = 600
    await db_session.flush()

    job_fast, reason = await create_voice_clone_job(
        db_session, user.id, text="မင်္ဂလာပါ", model_id=model.id, speed_preset="fast",
    )
    assert reason == "QUEUED" and job_fast is not None
    assert job_fast.speed_preset == "+20%"

    job_ultra, reason2 = await create_voice_clone_job(
        db_session, user2.id, text="မင်္ဂလာပါ", model_id=model.id, speed_preset="+25%",
    )
    assert reason2 == "QUEUED" and job_ultra is not None
    assert job_ultra.speed_preset == "+25%"


# ---------------------------------------------------------------------------
# 4. Cross-surface duration estimator & truncation alignment
# ---------------------------------------------------------------------------

def test_voice_and_tts_estimators_identical_and_monotonic():
    from app.services.queue import estimate_voice_job_seconds, speed_preset_factor
    from app.services.tts_service import estimate_tts_seconds

    txt = "က" * 1000
    ests = [estimate_voice_job_seconds(txt, s) for s in SPEED_OPTIONS]
    for i in range(len(ests) - 1):
        assert ests[i] > ests[i + 1]
    for opt in SPEED_OPTIONS:
        assert estimate_voice_job_seconds(txt, opt) == pytest.approx(estimate_tts_seconds(txt, opt), rel=1e-9)
    assert estimate_voice_job_seconds(txt, "slow") == pytest.approx(estimate_voice_job_seconds(txt, "-6%"), rel=1e-9)
    assert estimate_voice_job_seconds(txt, "normal") == pytest.approx(estimate_voice_job_seconds(txt, "+12%"), rel=1e-9)
    assert estimate_voice_job_seconds(txt, "fast") == pytest.approx(estimate_voice_job_seconds(txt, "+20%"), rel=1e-9)
    assert speed_preset_factor("+25%") == 1.25


def test_worker_truncation_threshold_scales_proportionally():
    from app.worker import _implausible_audio_reason

    txt = "က" * 1000
    # At +25% (1.25x): max_rate = 35 * (1.25 / 1.12) = 39.0625 chars/s -> 26s ok, 24s implausible
    assert _implausible_audio_reason(txt, 26, "+25%") is None
    assert _implausible_audio_reason(txt, 24, "+25%") is not None
    # '-6%' equals 'slow'; '+12%' equals 'normal'; '+20%' equals 'fast'
    assert _implausible_audio_reason(txt, 32, "-6%") is not None
    assert _implausible_audio_reason(txt, 32, "slow") is not None
    assert _implausible_audio_reason(txt, 35, "-6%") is None


# ---------------------------------------------------------------------------
# 5. Frontend JS runtime parity & 360px Padauk pixel-width check
# ---------------------------------------------------------------------------

def test_js_speed_helpers_match_python_if_node_available():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed on test host")

    # Extract pure speed block & estimateSpeechMinutesFromText from app.js
    m_head = re.search(
        r"(const VCP_SPEED_OPTIONS[\s\S]*?)// ── State",
        APP_JS,
    )
    m_est = re.search(
        r"(function estimateSpeechMinutesFromText\([\s\S]*?\n\})",
        APP_JS,
    )
    assert m_head and m_est
    script = (
        "const window = {};\n"
        + m_head.group(1)
        + "\n"
        + m_est.group(1)
        + "\n"
        + """
const out = {
    options: VCP_SPEED_OPTIONS,
    norms: ["slow", "normal", "fast", "+0%", "+8%", "+18%", "+22%", "-6%", "+12%", "+20%", "+25%", null, "junk"].map(x => [x, normalizeSpeedOption(x)]),
    tempos: ["slow", "normal", "fast", "-6%", "+0%", "+12%", "+20%", "+25%"].map(x => [x, speedTempoFactor(x)]),
    estMonotonic: ["-6%", "+0%", "+12%", "+20%", "+25%"].map(x => parseFloat(estimateSpeechMinutesFromText(2000, x))),
};
console.log(JSON.stringify(out));
"""
    )
    res = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)
    assert tuple(data["options"]) == SPEED_OPTIONS
    assert dict(data["norms"])["slow"] == "-6%"
    assert dict(data["norms"])["normal"] == "+12%"
    assert dict(data["norms"])["fast"] == "+20%"
    assert dict(data["norms"])["+8%"] == "+12%"
    assert dict(data["norms"])["+18%"] == "+20%"
    assert dict(data["norms"])["+22%"] == "+25%"
    assert dict(data["norms"])["junk"] == "+12%"
    for k, v in data["tempos"]:
        assert v == pytest.approx(resolve_speed_tempo(k), abs=1e-6)
    est = data["estMonotonic"]
    assert est == sorted(est, reverse=True)


def test_i18n_speed_keys_parity_and_360px_fit():
    font = ImageFont.truetype(str(ROOT / "app/webapp/static/fonts/Padauk-Bold.ttf"), 13)
    active5 = 268 * 2.4 / 6.4 - 6 - 2
    inactive5 = 268 / 6.4 - 6 - 2
    for key in ("speed_m6", "speed_0", "speed_12_capsule", "speed_20", "speed_25"):
        vals = re.findall(rf"\b{key}:\s*\"([^\"]+)\"", I18N_JS)
        assert len(vals) == 2, f"Expected my+en pair for {key}, found {vals}"
        for val in vals:
            assert font.getlength(val) <= active5, (key, val)
    for short in SPEED_OPTIONS:
        assert font.getlength(short) <= inactive5, short
