"""Unit tests for Gemini TTS Prompt-Shape Switch (Leak Phase 2, 2026-11-27).

Pins:
1. Dynamic setting ``gemini_tts_prompt_shape`` registered in ``Settings``, ``SETTING_TYPES``,
   ``DYNAMIC_SETTING_KEYS``, and ``CONFIG_SETTING_KEYS`` (excluded from ``HEARTBEAT_SETTING_KEYS``),
   with code default ``'c1'`` (probe winner: 0/40 leaks, 0/40 errors).
2. Exact request construction per arm (``a``, ``b``, ``c1``, ``c2``):
   - ``c1`` (default) : ``contents='[warm, lively storytelling] {cleaned}'``, ``system_instruction=None``
   - ``a``  (revert)  : byte-for-byte identical ``contents`` & ``GenerateContentConfig`` to legacy production
   - ``b``            : exact instruction line in ``config.system_instruction``, ``contents='[expressive storytelling] {cleaned}'``
   - ``c2``           : bare ``cleaned`` transcript in ``contents``, ``system_instruction=None``
3. Single unified builder across ``GeminiTTSEngine._ask()`` (recap, TTS tab, BYOK key check) and
   ``scripts/probe_tts_prompt_arms.py``, including leak-retry stripping of both ``contents`` and
   ``system_instruction`` down to bare ``c2`` after a rejected attempt.
4. Live propagation without restart via ``set_dynamic_setting``, ``load_dynamic_settings``,
   ``refresh_dynamic_settings_if_changed``, and sync-store ``botsetting_sync_read`` (with negative-cache TTL).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from google.genai import types
import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.config.settings import Settings
from app.models.schema import Base, BotSetting
from app.services import dynamic_settings as ds_mod
from app.services import key_rotation as kr_mod
from app.services import tts_leak_gate as gate_mod
from app.services.recap_engine import gemini_tts_engine as gtts
from scripts import probe_tts_prompt_arms as probe_mod


def _pcm_bytes(seconds: float = 1.8, sample_rate: int = 24000) -> bytes:
    samples = max(1, int(round(seconds * sample_rate)))
    arr = (np.sin(2.0 * np.pi * 220.0 * np.arange(samples) / sample_rate) * 6000).astype(np.int16)
    return arr.tobytes()


class _FakeResponse:
    def __init__(self, pcm: bytes):
        part = SimpleNamespace(inline_data=SimpleNamespace(data=pcm, mime_type="audio/pcm"))
        cand = SimpleNamespace(content=SimpleNamespace(parts=[part]))
        self.candidates = [cand]


@pytest.fixture(autouse=True)
def _isolate_prompt_shape_state(monkeypatch):
    kr_mod.health_for("gemini_tts").reset()
    gate_mod.reset_leak_gate_state_for_tests()
    gtts.reset_prompt_shape_cache()
    gtts._TTS_MODEL_CURSOR = 0
    monkeypatch.setattr(settings, "gemini_tts_prompt_shape", "c1")
    monkeypatch.setattr(settings, "gemini_tts_leak_gate_mode", "off")
    monkeypatch.setattr(kr_mod, "botsetting_sync_read", lambda _k: None)
    yield
    gtts.reset_prompt_shape_cache()
    gate_mod.reset_leak_gate_state_for_tests()
    kr_mod.health_for("gemini_tts").reset()


def test_dynamic_setting_registration_and_default_winner_c1(monkeypatch):
    """Pin registration in Settings + SETTING_TYPES + CONFIG_SETTING_KEYS and default == 'c1'."""
    field = Settings.model_fields["gemini_tts_prompt_shape"]
    assert field.default == "c1"
    assert field.alias == "GEMINI_TTS_PROMPT_SHAPE"

    assert "gemini_tts_prompt_shape" in ds_mod.SETTING_TYPES
    assert ds_mod.SETTING_TYPES["gemini_tts_prompt_shape"] is str
    assert "gemini_tts_prompt_shape" in ds_mod.DYNAMIC_SETTING_KEYS
    assert "gemini_tts_prompt_shape" in ds_mod.CONFIG_SETTING_KEYS
    assert "gemini_tts_prompt_shape" not in ds_mod.HEARTBEAT_SETTING_KEYS

    assert gtts.DEFAULT_GEMINI_TTS_PROMPT_SHAPE == "c1"
    assert gtts.VALID_GEMINI_TTS_PROMPT_SHAPES == ("a", "b", "c1", "c2")
    assert gtts.get_gemini_tts_prompt_shape() == "c1"

    # Case/whitespace normalization and safe fallback to 'c1' on invalid input
    for raw, expected in (
        ("a", "a"),
        (" A ", "a"),
        ("B", "b"),
        (" c1 ", "c1"),
        ("C2", "c2"),
        ("unknown_shape", "c1"),
        ("", "c1"),
    ):
        monkeypatch.setattr(settings, "gemini_tts_prompt_shape", raw)
        gtts.reset_prompt_shape_cache()
        assert gtts.get_gemini_tts_prompt_shape() == expected


def test_each_arm_request_construction_and_legacy_a_byte_for_byte():
    """Pin exact (contents, GenerateContentConfig) for arms c1 (default), a (legacy), b, and c2."""
    raw_text = "  [sigh] ဒီဇာတ်ကား (2026) ဟာ (laughs) မြန်မာ ပရိသတ်တွေအတွက် အရမ်းစိတ်ဝင်စားဖို့ ကောင်းပါတယ်။  "
    cleaned = gtts._clean_tts_text(raw_text)
    assert "(2026)" in cleaned
    assert "(laughs)" not in cleaned
    assert "[sigh]" not in cleaned

    # 1) Default (shape=None -> 'c1'): tag-only prefix, no English instruction prose, no system_instruction
    contents_def, cfg_def = gtts.build_gemini_tts_request(raw_text, "Charon")
    contents_c1, cfg_c1 = gtts.build_gemini_tts_request(raw_text, "gemini:Charon", shape="c1")
    assert contents_def == contents_c1 == f"[warm, lively storytelling] {cleaned}"
    assert cfg_def.system_instruction is None
    assert cfg_c1.system_instruction is None
    assert "Narrate as ONE" not in contents_c1
    assert "storyteller" not in contents_c1.lower()
    assert cfg_c1.response_modalities == ["AUDIO"]
    assert cfg_c1.speech_config.voice_config.prebuilt_voice_config.voice_name == "Charon"

    # 2) Arm 'a': byte-for-byte legacy production request shape (male Charon & female Kore)
    for voice_arg, canonical_voice, gender in (
        ("Charon", "Charon", "male"),
        ("gemini:Charon", "Charon", "male"),
        ("Kore", "Kore", "female"),
        ("gemini:Kore", "Kore", "female"),
    ):
        contents_a, cfg_a = gtts.build_gemini_tts_request(raw_text, voice_arg, shape="a")
        legacy_contents = (
            f"Narrate as ONE adult {gender} Burmese storyteller — warm, lively, varied pace, "
            "the same voice throughout. Read only the transcript below, exactly once, adding nothing.\n"
            f"[expressive storytelling] {cleaned}"
        )
        legacy_cfg = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=canonical_voice)
                )
            ),
        )
        assert contents_a.encode("utf-8") == legacy_contents.encode("utf-8")
        assert contents_a == gtts.narrator_prompt_short(raw_text, voice_arg)
        assert cfg_a.system_instruction is None
        assert cfg_a.model_dump(exclude_none=True) == legacy_cfg.model_dump(exclude_none=True)

    # 3) Arm 'b': exact instruction text in system_instruction, [expressive storytelling] in contents
    contents_b_m, cfg_b_m = gtts.build_gemini_tts_request(raw_text, "Charon", shape="b")
    contents_b_f, cfg_b_f = gtts.build_gemini_tts_request(raw_text, "Kore", shape="b")
    assert contents_b_m == f"[expressive storytelling] {cleaned}"
    assert contents_b_f == f"[expressive storytelling] {cleaned}"
    assert "Narrate as ONE" not in contents_b_m
    assert cfg_b_m.system_instruction == gtts.narrator_prompt_short(raw_text, "Charon").split("\n", 1)[0]
    assert cfg_b_f.system_instruction == gtts.narrator_prompt_short(raw_text, "Kore").split("\n", 1)[0]

    # 4) Arm 'c2': bare cleaned transcript only, zero tags, zero system_instruction
    contents_c2, cfg_c2 = gtts.build_gemini_tts_request(raw_text, "Charon", shape="c2")
    assert contents_c2 == cleaned
    assert cfg_c2.system_instruction is None
    assert "[" not in contents_c2

    # 5) Probe script delegates to the exact same canonical builder with zero divergence
    for arm in ("A", "B", "C1", "C2"):
        assert probe_mod.build_arm_request(arm, raw_text, "Charon") == gtts.build_gemini_tts_payload(
            raw_text, "Charon", shape=arm, strict_shape=True
        )
    with pytest.raises(ValueError, match="Unknown arm"):
        probe_mod.build_arm_request("Z9", raw_text, "Charon")


def test_engine_ask_routes_all_arms_and_strips_system_instruction_on_leak_retry(tmp_path, monkeypatch):
    """Pin GeminiTTSEngine._ask() payload + config across all 4 arms and bare-c2 stripping on retry."""
    seen: list[dict[str, Any]] = []
    scripted_durations: list[float] = []

    def _fake_client(api_key: str):
        def _gen(*, model: str, contents: str, config: Any = None):
            seen.append({"key": api_key, "model": model, "contents": contents, "config": config})
            dur = scripted_durations.pop(0) if scripted_durations else 1.8
            return _FakeResponse(_pcm_bytes(dur))

        return SimpleNamespace(models=SimpleNamespace(generate_content=_gen))

    monkeypatch.setattr(gtts, "_client", _fake_client)
    eng = gtts.GeminiTTSEngine(ffmpeg="ffmpeg", work_dir=tmp_path, keys=["k1", "k2"], log=lambda _m: None)
    sample = "ဒါက မြန်မာ စမ်းသပ် စာကြောင်း တိုလေး ဖြစ်ပါတယ်။"

    for shape_val, expected_contents, expect_sys in (
        ("c1", f"[warm, lively storytelling] {sample}", None),
        ("a", gtts.narrator_prompt_short(sample, "Charon"), None),
        ("b", f"[expressive storytelling] {sample}", gtts._short_instruction_line("Charon")),
        ("c2", sample, None),
    ):
        seen.clear()
        monkeypatch.setattr(settings, "gemini_tts_prompt_shape", shape_val)
        gtts.reset_prompt_shape_cache()
        eng._ask(sample, "Charon")
        assert len(seen) == 1
        assert seen[0]["contents"] == expected_contents
        assert seen[0]["config"].system_instruction == expect_sys

    # Even if shape='b' were active and attempt 1 leaked, attempt 2 MUST strip BOTH
    # contents AND config.system_instruction down to bare cleaned text (shape='c2')!
    seen.clear()
    monkeypatch.setattr(settings, "gemini_tts_prompt_shape", "b")
    gtts.reset_prompt_shape_cache()
    scripted_durations.extend([14.0, 1.8])  # 14.0s triggers duration guard reject on attempt 1
    eng._ask(sample, "Charon")
    assert len(seen) == 2
    assert seen[0]["config"].system_instruction == gtts._short_instruction_line("Charon")
    assert seen[0]["contents"] == f"[expressive storytelling] {sample}"
    assert seen[1]["config"].system_instruction is None
    assert seen[1]["contents"] == sample


@pytest.mark.asyncio
async def test_dynamic_setting_propagates_without_restart(tmp_path, monkeypatch):
    """Pin live propagation via set_dynamic_setting, load_dynamic_settings,
    refresh_dynamic_settings_if_changed, and sync-store reads without recreating GeminiTTSEngine."""
    db_file = tmp_path / "prompt_shape_live.db"
    async_eng = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}", future=True)
    async with async_eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=async_eng, class_=AsyncSession, expire_on_commit=False)

    seen_calls: list[tuple[str, str | None]] = []

    def _fake_client(_key: str):
        def _gen(*, model: str, contents: str, config: Any = None):
            seen_calls.append((contents, getattr(config, "system_instruction", None)))
            return _FakeResponse(_pcm_bytes(1.6))

        return SimpleNamespace(models=SimpleNamespace(generate_content=_gen))

    monkeypatch.setattr(gtts, "_client", _fake_client)
    # Single long-lived engine instance (simulating a running worker/web process — no restart!)
    tts_eng = gtts.GeminiTTSEngine(
        ffmpeg="ffmpeg", work_dir=tmp_path / "work", keys=["pool_k1"], log=lambda _m: None
    )
    text = "မိုးသက်လေပြင်းတွေ ကျနေတဲ့ ညတစ်ညမှာ လူငယ်တစ်ယောက် ရောက်လာခဲ့ပါတယ်။"

    ds_mod.reset_dynamic_settings_watermark()
    ds_mod.invalidate_dynamic_settings_cache()
    try:
        # 1) Initial default ('c1') before any DB row exists
        tts_eng._ask(text, "Charon")
        assert seen_calls[-1] == (f"[warm, lively storytelling] {text}", None)

        # 2) Admin / ops saves 'a' via set_dynamic_setting -> instant revert on next _ask() call!
        async with maker() as s:
            await ds_mod.set_dynamic_setting(s, "gemini_tts_prompt_shape", "a", admin_id=1)
            await s.commit()
        assert settings.gemini_tts_prompt_shape == "a"
        tts_eng._ask(text, "Charon")
        assert seen_calls[-1] == (gtts.narrator_prompt_short(text, "Charon"), None)

        # 3) Simulate another process starting with 'c1' in RAM and reloading DB via load_dynamic_settings
        monkeypatch.setattr(settings, "gemini_tts_prompt_shape", "c1")
        gtts.reset_prompt_shape_cache()
        async with maker() as s:
            loaded = await ds_mod.load_dynamic_settings(s, quiet=True)
        assert loaded["gemini_tts_prompt_shape"] == "a"
        assert settings.gemini_tts_prompt_shape == "a"
        tts_eng._ask(text, "Charon")
        assert seen_calls[-1] == (gtts.narrator_prompt_short(text, "Charon"), None)

        # 4) External SQL update to 'c2' picked up by worker watermark refresh_dynamic_settings_if_changed
        async with maker() as s:
            await ds_mod.refresh_dynamic_settings_if_changed(s, min_interval_s=0.0)
        async with maker() as s:
            row = await s.get(BotSetting, "gemini_tts_prompt_shape")
            row.value = "c2"
            await s.commit()
        async with maker() as s:
            reloaded = await ds_mod.refresh_dynamic_settings_if_changed(s, min_interval_s=0.0)
        assert reloaded is True
        assert settings.gemini_tts_prompt_shape == "c2"
        tts_eng._ask(text, "Charon")
        assert seen_calls[-1] == (text, None)

        # 5) Direct sync-store override (e.g. live Supabase SQL flip on vcp-web) + negative-cache TTL
        clock = {"t": 100.0}
        sync_reads = {"n": 0}
        sync_kv: dict[str, str] = {}

        def _counted_sync_read(key: str) -> str | None:
            if key == "gemini_tts_prompt_shape":
                sync_reads["n"] += 1
                return sync_kv.get(key)
            return None

        monkeypatch.setattr(gtts.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(kr_mod, "botsetting_sync_read", _counted_sync_read)
        monkeypatch.setattr(settings, "gemini_tts_prompt_shape", "c1")
        gtts.reset_prompt_shape_cache()

        # Two reads within 2s TTL when unset -> 1 DB read (_SHAPE_MISS_SENTINEL cached)
        assert gtts.get_gemini_tts_prompt_shape() == "c1"
        clock["t"] += 0.5
        assert gtts.get_gemini_tts_prompt_shape() == "c1"
        assert sync_reads["n"] == 1

        # SQL row flipped to 'a' -> observed after TTL expiry without restart
        sync_kv["gemini_tts_prompt_shape"] = "a"
        clock["t"] += 2.5
        tts_eng._ask(text, "Kore")
        assert seen_calls[-1] == (gtts.narrator_prompt_short(text, "Kore"), None)
    finally:
        ds_mod.reset_dynamic_settings_watermark()
        ds_mod.invalidate_dynamic_settings_cache()
        await async_eng.dispose()
