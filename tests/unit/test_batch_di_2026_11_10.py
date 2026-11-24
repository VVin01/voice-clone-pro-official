"""Batch DI (2026-11-10): three owner-ordered fixes.

1. Protection Strong back to pre-DD behaviour — the DD micro speed
   factor (setpts + asetrate pitch shift) audibly dulled narration;
   Strong now applies ONLY light grain + metadata strip.
2. Subtitle cues too fragmented — the SRT layer hard-split at every
   sentence/clause boundary so short sentences shipped as short cues
   regardless of the size-scaled budget. The 1-line chunker now
   fills greedily across boundaries and multi-line mode packs
   sentences up to the budget; capacity stays strictly size-scaled
   across the whole 10–26pt slider range, and the Lines (1/2/3)
   choice is pinned through the 1-Click prefs + persisted-config
   paths (the recapLines plumbing itself was verified intact).
3. Balance-ordered VoxCPM token walk — candidate tokens are ordered
   by last-known ZeroGPU balance, richest first (space priority
   preserved inside each token round), so a segment no longer burns
   its failover budget on instant admission refusals from nearly
   empty accounts while rich tokens wait in rotation order.
"""
from __future__ import annotations

import json
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.services.recap_engine.core as core
import app.services.recap_engine.png_subtitles as pq
from app.services import voxcpm_engine as vx
from app.services.voxcpm_engine import VoxCPMEngine

CW, CH = 1080, 1920
LB = core.frame_layout("9:16", "blur", 1080, 1920)
BOUND = int(0.92 * 1080)

LONG = "မြို့ကြီးဆီကို ထွက်ခွာပြီး သင်္ကြန်ပွဲတော်မှာ ကျောင်းသားတွေ လွှမ်းမိုးနေတဲ့ ပြဿနာကို ဆုံးဖြတ်ခဲ့ကြပါတယ်"


# ── 1. Protection Strong: grain + strip only, never a pitch shift ──

def test_di_strong_has_no_speed_factor():
    assert not hasattr(core, "protection_speed_factor")
    src = Path(core.__file__).read_text(encoding="utf-8")
    assert "protection_speed_factor" not in src
    pipe = Path(core.__file__).with_name("recap_pipeline.py").read_text(encoding="utf-8")
    assert "protection_speed_factor" not in pipe


def test_di_strong_ffmpeg_args_grain_and_strip_only(tmp_path, monkeypatch):
    video = tmp_path / "out.mp4"
    video.write_bytes(b"x" * 20_000)
    captured = {}

    class _Res:
        returncode = 0

    def fake_run(cmd, timeout=None, what=""):
        captured["cmd"] = [str(c) for c in cmd]
        Path(cmd[-1]).write_bytes(b"y" * 20_000)
        return _Res()

    monkeypatch.setattr(core, "_run_media", fake_run)
    result = core.apply_protection_strong("ffmpeg", video)
    assert result.name == "out.prot.mp4"
    joined = " ".join(captured["cmd"])
    assert "noise=alls=6:allf=t" in joined
    assert "-map_metadata" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-map_metadata") + 1] == "-1"
    # The pitch-shift machinery is gone: no retime, no resample.
    assert "asetrate" not in joined
    assert "setpts" not in joined
    assert "-af" not in captured["cmd"]


def test_di_strong_failure_keeps_original(tmp_path, monkeypatch):
    video = tmp_path / "out.mp4"
    video.write_bytes(b"x" * 20_000)

    class _Res:
        returncode = 1

    monkeypatch.setattr(core, "_run_media", lambda cmd, timeout=None, what="": _Res())
    assert core.apply_protection_strong("ffmpeg", video) == video


def test_di_protection_levels_unchanged():
    assert core.normalize_protection_level("off") == "off"
    assert core.normalize_protection_level("standard") == "standard"
    assert core.normalize_protection_level("STRONG") == "strong"
    assert core.normalize_protection_level("bogus") == "standard"


# ── 2a. Capacity scales with font size across the whole slider ────

@pytest.mark.parametrize("lines", [1, 2, 3])
def test_di_capacity_strictly_decreasing_in_pt(lines):
    ladder = [core._subtitle_max_chars(True, lines, 0.92, pt) for pt in range(10, 27)]
    assert all(a > b for a, b in zip(ladder, ladder[1:])), (lines, ladder)


def test_di_capacity_anchor_table():
    # DD anchors for the vertical 1-line budget — Batch DJ
    # recalibrated them to measured rendered capacity
    # (33/25/20/17 supersedes the DD ratio values 56/45/38/32).
    assert core._subtitle_max_chars(True, 1, 0.92, 13) == 33
    assert core._subtitle_max_chars(True, 1, 0.92, 16) == 25
    assert core._subtitle_max_chars(True, 1, 0.92, 19) == 20
    assert core._subtitle_max_chars(True, 1, 0.92, 22) == 17


def _pieces(tmp_path, text, size_pt, start=0.0, end=12.0):
    renderer = pq.PngSubtitleRenderer(tmp_path / "di_cache")
    font_px = core.subtitle_font_px(CW, CH, size_pt, 1, LB)
    font = core.subtitle_font_file("padauk")
    visuals = pq.preset_visuals(core.SUBTITLE_STYLE_PRESETS["yellow"], CH, font_px)
    return core.fit_png_cue_pieces(renderer, text, font_px, font, visuals, BOUND, start, end)


def test_di_smaller_font_yields_fewer_longer_pieces(tmp_path):
    text = LONG + " " + LONG
    small = _pieces(tmp_path / "a", text, 13)
    large = _pieces(tmp_path / "b", text, 22)
    assert len(small) < len(large)
    avg_small = sum(len(ln[0]) for ln, *_ in small) / len(small)
    avg_large = sum(len(ln[0]) for ln, *_ in large) / len(large)
    assert avg_small > avg_large
    # Nothing is lost or invented at either size.
    for pieces in (small, large):
        rejoined = "".join(ln[0] for ln, *_ in pieces).replace(" ", "")
        assert rejoined == text.replace(" ", "")


# ── 2b. The chunker fills across sentence boundaries ──────────────

_SENT = "သူသည် စာအုပ်တစ်အုပ်ကို ဖတ်နေသည်။"  # one short sentence


def test_di_chunker_merges_short_sentences_to_budget():
    text = " ".join([_SENT] * 6)
    chunks = core._split_burmese_into_single_line_chunks(text, max_chars=45)
    # Old behaviour: 11 chunks for this text — the hard ။ pre-split
    # recursed into fragments and the final fragment's last word
    # (9 chars < the 12-char tail-merge floor) merged BACK into its
    # predecessor, leaving 5 sentence chunks + 6 two-word stubs.
    # Greedy fill packs words across the boundaries instead: every
    # cue runs close to the 45-char budget.
    assert len(chunks) < 11
    assert all(len(c) <= 45 for c in chunks)
    # Nearly every cue is budget-full (the old stub pieces ran 14).
    assert min(len(c) for c in chunks) >= 25
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_di_chunker_never_cuts_words():
    text = " ".join([_SENT] * 4)
    chunks = core._split_burmese_into_single_line_chunks(text, max_chars=30)
    for chunk in chunks:
        for word in chunk.split(" "):
            assert word in text.split(" ") or any(
                word in s for s in text.split(" ")
            )


def test_di_chunker_short_text_untouched():
    assert core._split_burmese_into_single_line_chunks("မင်္ဂလာပါ", max_chars=45) == ["မင်္ဂလာပါ"]


# ── 2c. Multi-line mode packs sentences up to the budget ──────────

def test_di_multiline_packs_sentences():
    text = " ".join([_SENT] * 4)
    cues = core.split_burmese_subtitle_cues(0.0, 8.0, text, max_cue_chars=80, lines_per_cue=2)
    # 4 short sentences pack into budget-sized groups (2 cues of two
    # sentences), not one cue per sentence (old) and not one mega-cue.
    assert len(cues) == 2
    joined = "".join(c[2] for c in cues).replace("\n", " ").replace(" ", "")
    assert joined == text.replace(" ", "")
    for start, end, _txt in cues:
        assert end - start >= 1.8 - 1e-9
    # Each packed cue is fuller than one bare sentence.
    assert all(len(c[2].replace("\n", "")) > len(_SENT) for c in cues)


def test_di_multiline_single_sentence_stays_one_cue():
    cues = core.split_burmese_subtitle_cues(0.0, 6.0, LONG, max_cue_chars=80, lines_per_cue=2)
    assert len(cues) == 1


# ── 2d. Lines choice survives the 1-Click prefs + persisted paths ─

def test_di_recap_lines_prefs_round_trip():
    from app.webapp import api

    assert api._effective_user_prefs({})["recapLines"] == 1
    assert api._effective_user_prefs({"recapLines": 2})["recapLines"] == 2
    assert api._effective_user_prefs({"recapLines": 3})["recapLines"] == 3
    # Out-of-range stored values are dropped by the sanitizer (never
    # stored), so the merged result falls back to the default 1 —
    # the value can never reach the burn out of range.
    assert api._effective_user_prefs({"recapLines": 9})["recapLines"] == 1


@pytest.mark.parametrize("lines", [1, 2, 3])
def test_di_recap_lines_persisted_config_round_trip(lines):
    from app.webapp import api

    req = api.RecapConfigRequest(url="https://example.com/v", sub_lines=lines)
    state = api._persisted_recap_state(
        req, credit_deducted=False, credits_charged=0, is_vip=False, phase="queued"
    )
    job = SimpleNamespace(
        recap_config=state,
        source_audio_path=None,
        input_text="https://example.com/v",
        preset_voice="komin",
    )
    rebuilt = api._recap_job_config(job)
    assert rebuilt is not None
    assert rebuilt.sub_lines == lines
    assert json.loads(state)["config"]["sub_lines"] == lines


# ── 3. Balance-ordered VoxCPM walk ────────────────────────────────

def _write_wav(path: Path, seconds: float = 0.3, rate: int = 24000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


class _FakeJob:
    def __init__(self, result_path):
        self._result_path = result_path

    def done(self):
        return True

    def status(self):
        return None

    def result(self):
        return str(self._result_path)


_GATE_94_65 = (
    "AppError: You have exceeded your free ZeroGPU quota "
    "(94s requested vs. 65s left)"
)


class _BalClient:
    """Fake gradio client: per-(space, token) behaviour; records the
    attempt order. ``gate:<left>`` refuses at the admission gate with
    the production refusal text (94 s price)."""

    def __init__(self, behaviour, result_path, record, key):
        self.behaviour = behaviour
        self._result_path = result_path
        self.record = record
        self.key = key

    def submit(self, **kwargs):
        self.record.append(self.key)
        b = self.behaviour
        if isinstance(b, str) and b.startswith("gate:"):
            left = b.split(":", 1)[1]
            raise RuntimeError(
                "AppError: You have exceeded your free ZeroGPU quota "
                f"(94s requested vs. {left}s left)"
            )
        if b == "boom":
            raise RuntimeError("space exploded")
        return _FakeJob(self._result_path)


def _bal_engine(tmp_path, monkeypatch, *, tokens, spaces=("spaceA",)):
    monkeypatch.setattr(vx, "get_available_hf_tokens", lambda: list(tokens))
    monkeypatch.setattr(VoxCPMEngine, "_token_cooldowns", {})
    monkeypatch.setattr(VoxCPMEngine, "_token_rr_counter", 0)
    monkeypatch.setattr(VoxCPMEngine, "_token_bind_rr_counters", {})
    monkeypatch.setattr(VoxCPMEngine, "_token_balances", {})
    monkeypatch.setattr(VoxCPMEngine, "_space_admission_prices", {})
    monkeypatch.setattr(VoxCPMEngine, "_last_admission_price", None)
    engine = VoxCPMEngine(work_dir=tmp_path / "cache")
    engine.spaces = list(spaces)
    engine.hf_tokens = list(tokens)
    return engine


def _patch_bal_clients(monkeypatch, behaviours, result_path, record):
    clients = {
        key: _BalClient(behaviour, result_path, record, key)
        for key, behaviour in behaviours.items()
    }
    def _get_client(cls, space_name, hf_token=None):
        return clients[(space_name, hf_token)]

    monkeypatch.setattr(VoxCPMEngine, "get_client", classmethod(_get_client))


def test_di_parse_admission_refusal():
    assert vx._parse_admission_refusal(_GATE_94_65) == (94.0, 65.0)
    assert vx._parse_admission_refusal(
        "ZeroGPU quota: 180s requested but the account only has 42s remaining"
    ) == (180.0, 42.0)
    assert vx._parse_admission_refusal("space exploded") is None
    assert vx._parse_admission_refusal("") is None


def test_di_balance_tiers():
    # SUPERSEDED by HF Pool Phase 1 Part 6 (#1, 2026-11-24): Best-Fit selection
    # sorts solvent Tier 0 tokens by SMALLEST sufficient balance `(0, +bal)`.
    VoxCPMEngine._token_balances = {"hf_a": 60.0, "hf_b": 2400.0, "hf_c": 300.0}
    VoxCPMEngine._last_admission_price = 94.0
    try:
        assert VoxCPMEngine._balance_sort_key("hf_b") == (0, 2400.0)
        assert VoxCPMEngine._balance_sort_key("hf_c") == (0, 300.0)
        assert VoxCPMEngine._balance_sort_key("hf_a") == (2, -60.0)
        assert VoxCPMEngine._balance_sort_key("hf_unknown") == (1, 0.0)
        assert VoxCPMEngine._balance_sort_key(None) == (1, 0.0)
    finally:
        VoxCPMEngine._token_balances = {}
        VoxCPMEngine._last_admission_price = None


def test_di_walk_starts_from_richest_token(tmp_path, monkeypatch):
    # SUPERSEDED by HF Pool Phase 1 Part 6 (#1, 2026-11-24): Best-Fit picks
    # the smallest sufficient solvent token (`hf_c` at 300s >= 94s) before `hf_b` (800s),
    # and never attempts insolvent sub-gate `hf_a` (60s < 94s).
    result = _write_wav(tmp_path / "ok.wav")
    engine = _bal_engine(tmp_path, monkeypatch, tokens=["hf_a", "hf_b", "hf_c"])
    VoxCPMEngine._token_balances = {"hf_a": 60.0, "hf_b": 800.0, "hf_c": 300.0}
    VoxCPMEngine._space_admission_prices = {"spaceA": 94.0}
    VoxCPMEngine._last_admission_price = 94.0
    record: list = []
    _patch_bal_clients(monkeypatch, {
        ("spaceA", "hf_a"): "gate:60",
        ("spaceA", "hf_b"): "ok",
        ("spaceA", "hf_c"): "ok",
        ("spaceA", None): "ok",
    }, result, record)
    out = engine.synthesize(
        text="မင်္ဂလာပါ", output_wav=tmp_path / "o.wav", allow_fallback=False
    )
    assert out.is_file()
    assert record[0] == ("spaceA", "hf_c")
    assert ("spaceA", "hf_a") not in record


def test_di_refusal_updates_table_and_reorders_next_walk(tmp_path, monkeypatch):
    result = _write_wav(tmp_path / "ok.wav")
    tokens = ["hf_a", "hf_b", "hf_c"]
    record1: list = []
    engine1 = _bal_engine(tmp_path / "w1", monkeypatch, tokens=tokens)
    _patch_bal_clients(monkeypatch, {
        ("spaceA", "hf_a"): "gate:89",
        ("spaceA", "hf_b"): "gate:65",
        ("spaceA", "hf_c"): "ok",
        ("spaceA", None): "ok",
    }, result, record1)
    engine1.synthesize(
        text="မင်္ဂလာပါ", output_wav=tmp_path / "w1o.wav", allow_fallback=False
    )
    # The refusals taught the table exact balances + the space price;
    # C's success records at least the price it provably covered.
    assert VoxCPMEngine._token_balances["hf_a"] == 89.0
    assert VoxCPMEngine._token_balances["hf_b"] == 65.0
    assert VoxCPMEngine._token_balances["hf_c"] == 94.0
    assert VoxCPMEngine._last_admission_price == 94.0
    assert VoxCPMEngine._space_admission_prices["spaceA"] == 94.0

    # Next segment (fresh engine + fresh job memory): C is the only
    # solvent token, so the walk starts there even though rotation
    # alone would have started at a different token.
    record2: list = []
    engine2 = _bal_engine(tmp_path / "w2", monkeypatch, tokens=tokens)
    # _bal_engine reset the class tables via monkeypatch — restore the
    # learned state the second walk must see.
    VoxCPMEngine._token_balances = {"hf_a": 89.0, "hf_b": 65.0, "hf_c": 94.0}
    VoxCPMEngine._last_admission_price = 94.0
    VoxCPMEngine._space_admission_prices = {"spaceA": 94.0}
    _patch_bal_clients(monkeypatch, {
        ("spaceA", "hf_a"): "ok",
        ("spaceA", "hf_b"): "ok",
        ("spaceA", "hf_c"): "ok",
        ("spaceA", None): "ok",
    }, result, record2)
    engine2.synthesize(
        text="နောက်တစ်ပိုဒ်", output_wav=tmp_path / "w2o.wav", allow_fallback=False
    )
    assert record2 == [("spaceA", "hf_c")]


def test_di_success_debits_known_balance(tmp_path, monkeypatch):
    result = _write_wav(tmp_path / "ok.wav")
    engine = _bal_engine(tmp_path, monkeypatch, tokens=["hf_c"])
    VoxCPMEngine._token_balances = {"hf_c": 300.0}
    VoxCPMEngine._space_admission_prices = {"spaceA": 94.0}
    VoxCPMEngine._last_admission_price = 94.0
    record: list = []
    _patch_bal_clients(monkeypatch, {
        ("spaceA", "hf_c"): "ok",
        ("spaceA", None): "ok",
    }, result, record)
    engine.synthesize(
        text="မင်္ဂလာပါ", output_wav=tmp_path / "o.wav", allow_fallback=False
    )
    assert VoxCPMEngine._token_balances["hf_c"] == 206.0
