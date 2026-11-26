"""Unit tests for Section A — Gemini TTS Prompt Leak Phase 0 + Phase 1 Hardening (2026-11-24).

Pins:
1. 23-key pool fixture: after exactly 2 leak rejects on primary (`gemini-3.8-flash-tts`),
   the next attempt breaks out of the key loop and moves to `gemini-3.8-flash-lite-tts`.
2. Attempt 2 after a leak reject sends the bare cleaned transcript (zero instruction tokens).
3. Total attempts never exceed `GEMINI_TTS_MAX_ATTEMPTS_PER_CALL` (12); leak retries never exceed 4.
4. Per-chunk leak exhaustion does NOT set `self.exhausted` or BYOK `RoutedGeminiEngine._own_spent`.
5. BYOK engine (`record_usage=False`) writes zero rows to the pool ledger (`aj_kh:gemini_tts`) on leaks;
   pool engine (`record_usage=True`) charges daily RPD usage on leak-rejected HTTP 200s.
6. Head content gate (`tts_leak_gate.py`): fingerprint subtraction of user ASCII words,
   1-edit fuzzy match on >=7-char terms, Myanmar-opening unexpected-English rule,
   fail-open on Whisper exception, `off | shadow | enforce` mode switch, and Recap risk-tiering.
7. Leak sample ring buffer eviction at 20 entries and 8 MB cap, sanitized `meta.json`.
8. Phase 0 probe script (`scripts/probe_tts_prompt_arms.py`) imports cleanly and `--list`
   enumerates 4 arms x 2 models x 10 chunks = 80 calls with zero network calls.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from app.config import settings
from app.services import key_rotation as kr
from app.services import tts_leak_gate as gate
from app.services.recap_engine import gemini_tts_engine as gtts


def _pcm_bytes(seconds: float, sample_rate: int = 24000) -> bytes:
    samples = max(1, int(round(seconds * sample_rate)))
    arr = (np.sin(2.0 * np.pi * 220.0 * np.arange(samples) / sample_rate) * 8000).astype(np.int16)
    return arr.tobytes()


def _write_dummy_wav(path: Path, seconds: float = 1.5, sample_rate: int = 24000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(_pcm_bytes(seconds, sample_rate))
    return path


class _FakeInlineData:
    def __init__(self, data: bytes, mime_type: str = "audio/pcm"):
        self.data = data
        self.mime_type = mime_type


class _FakeResponse:
    def __init__(self, pcm: bytes):
        part = SimpleNamespace(inline_data=_FakeInlineData(pcm))
        cand = SimpleNamespace(content=SimpleNamespace(parts=[part]))
        self.candidates = [cand]


@pytest.fixture(autouse=True)
def _isolate_tts_state(monkeypatch, tmp_path):
    kr.health_for("gemini_tts").reset()
    gate.reset_leak_gate_state_for_tests()
    monkeypatch.setattr(gate, "_default_sample_dir", lambda: tmp_path / "tts_leak_samples")
    monkeypatch.setattr(settings, "gemini_tts_leak_gate_mode", "off")
    monkeypatch.setattr(gtts, "BYOK_SINGLE_KEY_STAGGER_SECONDS", 0.0)
    yield
    gate.reset_leak_gate_state_for_tests()
    kr.health_for("gemini_tts").reset()


def test_23_key_pool_escalates_to_lite_after_two_leak_rejects(tmp_path, monkeypatch):
    """Pin #1 & #2: 23-key pool breaks out of primary key loop after 2 leak rejects -> lite, bare text on retry."""
    pool_keys = [f"gem_pool_key_{i + 1}" for i in range(23)]
    monkeypatch.setattr(gtts, "collect_gemini_keys", lambda: list(pool_keys))

    seen_calls: list[tuple[str, str, str]] = []

    def _fake_client(api_key: str):
        def _gen(*, model: str, contents: str, config: Any = None):
            seen_calls.append((api_key, model, contents))
            if model == "gemini-3.8-flash-tts":
                # 12.0s audio for short Burmese text -> duration guard leak reject!
                return _FakeResponse(_pcm_bytes(12.0))
            # Lite returns clean 2.0s audio
            return _FakeResponse(_pcm_bytes(2.0))

        return SimpleNamespace(models=SimpleNamespace(generate_content=_gen))

    monkeypatch.setattr(gtts, "_client", _fake_client)
    eng = gtts.GeminiTTSEngine(ffmpeg="ffmpeg", work_dir=tmp_path)
    cleaned = "ဒါက မြန်မာ စမ်းသပ် စာသား တိုတိုလေး ဖြစ်ပါတယ်။"

    pcm_bytes = eng._ask(cleaned, "Charon")
    assert len(pcm_bytes) == len(_pcm_bytes(2.0))

    # Exactly 2 attempts on primary, then attempt 3 is on lite!
    assert len(seen_calls) == 3
    assert seen_calls[0][1] == "gemini-3.8-flash-tts"
    assert seen_calls[1][1] == "gemini-3.8-flash-tts"
    assert seen_calls[2][1] == "gemini-3.8-flash-lite-tts"

    # Attempt 1 used default prompt shape c1 ([warm, lively storytelling]); attempts 2 & 3 stripped to bare cleaned transcript!
    assert seen_calls[0][2] == f"[warm, lively storytelling] {cleaned}"
    assert seen_calls[1][2] == cleaned
    assert seen_calls[2][2] == cleaned
    assert "storytelling" not in seen_calls[1][2].lower()
    assert "[" not in seen_calls[1][2]


def test_leak_retry_cap_4_and_attempt_cap_12_and_exhaustion_isolation(tmp_path, monkeypatch):
    """Pin #3 & #4: leak retries capped at 4 (<= 12 total); self.exhausted & BYOK _own_spent stay False."""
    pool_keys = [f"gem_pool_key_{i + 1}" for i in range(23)]
    monkeypatch.setattr(gtts, "collect_gemini_keys", lambda: list(pool_keys))

    calls: list[tuple[str, str]] = []

    def _always_leaky_client(api_key: str):
        def _gen(*, model: str, contents: str, config: Any = None):
            calls.append((api_key, model))
            return _FakeResponse(_pcm_bytes(14.0))

        return SimpleNamespace(models=SimpleNamespace(generate_content=_gen))

    monkeypatch.setattr(gtts, "_client", _always_leaky_client)
    eng = gtts.GeminiTTSEngine(ffmpeg="ffmpeg", work_dir=tmp_path)
    short_my = "စမ်းသပ် စာကြောင်း တစ်ကြောင်း ဖြစ်ပါတယ်။"

    with pytest.raises(gtts.GeminiTTSLeakExhaustedError):
        eng._ask(short_my, "Kore")

    # 2 on primary + 2 on lite = 4 attempts (<= 1 + MAX_LEAK_RETRIES_PER_CALL <= 12)
    assert len(calls) == 4
    assert len(calls) <= gtts.GEMINI_TTS_MAX_ATTEMPTS_PER_CALL
    assert [m for _, m in calls] == [
        "gemini-3.8-flash-tts",
        "gemini-3.8-flash-tts",
        "gemini-3.8-flash-lite-tts",
        "gemini-3.8-flash-lite-tts",
    ]
    # Per-chunk leak exhaustion must NEVER poison engine.exhausted for the rest of the job!
    assert eng.exhausted is False

    # Check BYOK 1-key RoutedGeminiEngine: 2 leak rejects per model on the single key,
    # and _own_spent stays False!
    calls.clear()
    own_eng = gtts.GeminiTTSEngine(
        ffmpeg="ffmpeg",
        work_dir=tmp_path / "byok",
        keys=["byok_secret_key_1"],
        record_usage=False,
    )
    fb_wav = _write_dummy_wav(tmp_path / "pool_fb.wav", 1.5)
    fallback_eng = SimpleNamespace(
        exhausted=False,
        generate_channel99_voice=lambda text, voice: fb_wav,
    )
    routed = gtts.RoutedGeminiEngine(own_engine=own_eng, pool_engine=fallback_eng)
    res_wav = routed.generate_channel99_voice(short_my, "Charon")
    assert res_wav == fb_wav
    assert len(calls) == 4  # 2 on primary + 2 on lite on the single BYOK key!
    assert own_eng.exhausted is False
    assert routed._own_spent is False


def test_byok_writes_zero_pool_ledger_rows_while_pool_charges_rpd(tmp_path, monkeypatch):
    """Pin #5: BYOK writes zero rows to pool health ledger; pool engine charges daily RPD on rejected HTTP 200s."""
    health = kr.health_for("gemini_tts")
    health.reset()

    def _leaky_client(_api_key: str):
        return SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **kw: _FakeResponse(_pcm_bytes(13.0))
            )
        )

    monkeypatch.setattr(gtts, "_client", _leaky_client)
    short_my = "စမ်းသပ် စာကြောင်း တစ်ကြောင်း ဖြစ်ပါတယ်။"

    # 1) BYOK engine (record_usage=False)
    byok = gtts.GeminiTTSEngine(
        ffmpeg="ffmpeg",
        work_dir=tmp_path / "byok",
        keys=["byok_only_key"],
        record_usage=False,
    )
    with pytest.raises(gtts.GeminiTTSLeakExhaustedError):
        byok._ask(short_my, "Charon")

    snap_after_byok = health.snapshot_state()
    assert snap_after_byok.get("usage", {}) == {}
    assert snap_after_byok.get("tts_quality", {}) == {}

    # 2) Pool engine (record_usage=True) with 2 pool keys -> 2 attempts on primary + 2 on lite = 4 RPD charges
    monkeypatch.setattr(gtts, "collect_gemini_keys", lambda: ["pool_key_alpha", "pool_key_beta"])
    pool_eng = gtts.GeminiTTSEngine(ffmpeg="ffmpeg", work_dir=tmp_path / "pool", record_usage=True)
    with pytest.raises(gtts.GeminiTTSLeakExhaustedError):
        pool_eng._ask(short_my, "Charon")

    # 2 attempts on primary (1 per key) + 2 on lite (1 per key) -> all 4 charged daily RPD usage!
    assert health.remaining("pool_key_alpha", "gemini-3.8-flash-tts") == 9
    assert health.remaining("pool_key_beta", "gemini-3.8-flash-tts") == 9
    assert health.remaining("pool_key_alpha", "gemini-3.8-flash-lite-tts") == 9
    assert health.remaining("pool_key_beta", "gemini-3.8-flash-lite-tts") == 9
    st_primary = health.tts_model_stats("gemini-3.8-flash-tts")
    st_lite = health.tts_model_stats("gemini-3.8-flash-lite-tts")
    assert st_primary["dq_reject"] == 2 and st_primary["accept"] == 0
    assert st_lite["dq_reject"] == 2 and st_lite["accept"] == 0


def test_head_gate_fingerprint_subtraction_myanmar_ratio_modes_and_fail_open(tmp_path, monkeypatch, caplog):
    """Pin #6: fingerprint subtraction, fuzzy match, Myanmar opening rule, fail-open, and shadow/enforce/off modes."""
    # (a) User text containing English prompt words ("storyteller", "transcript", "expressive") MUST NOT trigger!
    user_text_with_keywords = (
        "ဒီဇာတ်လမ်းထဲက storyteller က expressive ဖြစ်ပြီး transcript ကို ဖတ်ပြနေပါတယ်။"
    )
    v_clean = gate.evaluate_transcript_leak(
        "storyteller expressive transcript",
        cleaned_text=user_text_with_keywords,
        detected_lang="my",
        detected_lang_prob=0.92,
    )
    assert v_clean.verdict == "clean"

    # (b) Leaked instruction words NOT in user text (including 1-edit fuzzy on >=7-char terms) DO trigger!
    pure_burmese = "မောင်မောင်သည် မနက်စောစော ထပြီး ကျောင်းသို့ လမ်းလျှောက်သွားခဲ့ပါသည်။ " * 4
    v_fuzzy = gate.evaluate_transcript_leak(
        "You are a lively Burmese movie recap storytelir narating",
        cleaned_text=pure_burmese,
        detected_lang="en",
        detected_lang_prob=0.95,
    )
    assert v_fuzzy.verdict == "leak"
    assert "fingerprint" in v_fuzzy.reason

    # (c) Unexpected English on >85% Myanmar opening (prob >= 0.80, >=4 non-user English words)
    v_unexp_en = gate.evaluate_transcript_leak(
        "Please read this passage slowly and clearly today",
        cleaned_text=pure_burmese,
        detected_lang="en",
        detected_lang_prob=0.88,
    )
    assert v_unexp_en.verdict == "leak"
    assert "unexpected_en" in v_unexp_en.reason

    # Subtracting user's own English words prevents false positive under rule (b) too
    mixed_burmese_head = pure_burmese[:90] + " Apple Banana Cherry Dragonfruit"
    v_mixed_ok = gate.evaluate_transcript_leak(
        "Apple Banana Cherry Dragonfruit",
        cleaned_text=mixed_burmese_head,
        detected_lang="en",
        detected_lang_prob=0.90,
    )
    assert v_mixed_ok.verdict == "clean"

    # (d) Recap risk-tiering: >= 160 chars always verified; < 160 verified only if raw_secs > chars/15 + 1.2
    assert gate.should_verify_chunk(cleaned_chars=160, raw_secs=3.0) is True
    assert gate.should_verify_chunk(cleaned_chars=400, raw_secs=18.0) is True
    assert gate.should_verify_chunk(cleaned_chars=90, raw_secs=6.5) is False  # 90/15 + 1.2 = 7.2
    assert gate.should_verify_chunk(cleaned_chars=90, raw_secs=7.5) is True

    # (e) Fail-open when Whisper model raises on load/inference (logs once, returns clean)
    def _broken_whisper():
        raise RuntimeError("ctranslate2 cpu init boom")

    res_open = gate.verify_tts_pcm_head(
        _pcm_bytes(3.0), cleaned_text=pure_burmese, whisper_loader=_broken_whisper
    )
    assert res_open.verdict == "clean"
    assert "fail_open" in res_open.reason

    # (f) Shadow vs Enforce inside GeminiTTSEngine._ask()
    monkeypatch.setattr(gtts, "collect_gemini_keys", lambda: ["k1", "k2"])
    call_prompts: list[str] = []

    def _ok_duration_client(_key: str):
        def _gen(*, model: str, contents: str, config: Any = None):
            call_prompts.append(contents)
            return _FakeResponse(_pcm_bytes(12.0))  # 200+ char text -> passes duration guard!

        return SimpleNamespace(models=SimpleNamespace(generate_content=_gen))

    monkeypatch.setattr(gtts, "_client", _ok_duration_client)

    leak_verdict = gate.LeakGateResult(
        verdict="leak",
        reason="fingerprint:bigrams=burmese_storyteller",
        head_transcript="You are a lively Burmese storyteller",
        detected_language="en",
        language_probability=0.96,
    )
    clean_verdict = gate.LeakGateResult(
        verdict="clean",
        reason="clean",
        head_transcript="",
        detected_language="my",
        language_probability=0.94,
    )

    # Shadow mode: logs shadow_leak, accepts on attempt 1!
    gate.reset_leak_gate_state_for_tests()
    monkeypatch.setattr(settings, "gemini_tts_leak_gate_mode", "shadow")
    monkeypatch.setattr(gate, "verify_tts_pcm_head", lambda pcm, **kw: leak_verdict)
    eng = gtts.GeminiTTSEngine(ffmpeg="ffmpeg", work_dir=tmp_path)
    with caplog.at_level(logging.INFO):
        eng._ask(pure_burmese, "Charon")
    assert len(call_prompts) == 1
    assert "shadow_leak" in caplog.text

    # Enforce mode: rejects attempt 1, strips prompt on attempt 2, accepts when attempt 2 is clean!
    call_prompts.clear()
    gate.reset_leak_gate_state_for_tests()
    verdicts = iter([leak_verdict, clean_verdict])
    monkeypatch.setattr(settings, "gemini_tts_leak_gate_mode", "enforce")
    monkeypatch.setattr(gate, "verify_tts_pcm_head", lambda pcm, **kw: next(verdicts))
    eng._ask(pure_burmese, "Charon")
    assert len(call_prompts) == 2
    assert call_prompts[1] == pure_burmese.strip()


def test_ring_buffer_evicts_at_20_entries_and_8mb_and_sanitizes_meta(tmp_path):
    """Pin #7: ring buffer keeps <= 20 WAVs and <= 8 MB total, oldest evicted first, meta sanitized."""
    ring_dir = tmp_path / "ring_test"
    pcm_2s = _pcm_bytes(2.0)

    for idx in range(25):
        gate.record_leak_sample(
            pcm_2s,
            model="gemini-3.8-flash-tts",
            voice="Charon",
            cleaned_text=f"Chunk-{idx:02d}-" + ("က" * 80) + " SECRET_TAIL_NEVER_STORED",
            pcm_secs=2.0,
            reason=f"dq_reject_{idx}",
            sample_dir=ring_dir,
        )

    wavs = sorted(ring_dir.glob("leak_*.wav"))
    metas = sorted(ring_dir.glob("leak_*.meta.json"))
    assert len(wavs) == 20
    assert len(metas) == 20
    parsed_metas = [json.loads(p.read_text(encoding="utf-8")) for p in metas]
    reasons = [m["reason"] for m in parsed_metas]
    assert "dq_reject_0" not in reasons
    assert "dq_reject_24" in reasons
    for entry in parsed_metas:
        assert len(entry["text_head"]) <= 40
        assert "SECRET_TAIL_NEVER_STORED" not in json.dumps(entry)
        assert "key" not in entry and "user_id" not in entry

    # Test 8 MB byte cap eviction with synthetic ~580 KB 12s WAVs (8 MB fits at most 13 pairs)
    big_pcm = b"\x01\x00" * 288_000  # 12.0s @ 24kHz = 576 KB per WAV
    ring_mb = tmp_path / "ring_mb"
    for idx in range(18):
        gate.record_leak_sample(
            big_pcm,
            model="gemini-3.8-flash-tts",
            voice="Kore",
            cleaned_text="စမ်းသပ်",
            pcm_secs=12.0,
            reason=f"big_{idx}",
            sample_dir=ring_mb,
        )
    total_bytes = sum(p.stat().st_size for p in ring_mb.iterdir() if p.is_file())
    assert total_bytes <= gate.LEAK_RING_MAX_BYTES
    assert len(list(ring_mb.glob("leak_*.wav"))) < 18


def test_probe_script_dry_run_enumerates_arms_and_models():
    """Pin #8: standalone Phase 0 probe script imports & enumerates 4 arms x 2 models x 10 chunks = 80 calls."""
    proc = subprocess.run(
        [sys.executable, "scripts/probe_tts_prompt_arms.py", "--list"],
        capture_output=True,
        text=True,
        check=True,
    )
    out = proc.stdout
    assert "A, B, C1, C2" in out
    assert "gemini-3.8-flash-tts" in out
    assert "gemini-3.8-flash-lite-tts" in out
    assert "Planned API calls : 80" in out
    assert "python scripts/probe_tts_prompt_arms.py [--arms A,B] [--models ...]" in out
