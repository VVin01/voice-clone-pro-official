"""Tests for sentence/clause-first Burmese subtitle cue segmentation and
display-only stripping of Burmese punctuation (၊/။) across burn, SRT, and ASS
outputs while preserving punctuation in TTS/narration scripts.
"""

from __future__ import annotations

import json
import math
import subprocess
import wave
from pathlib import Path
from typing import Any

import pytest

from app.services.recap_engine import core, png_subtitles as _png_subs, recap_pipeline
from app.webapp import api as api_mod


# ── Golden Job rc_1795337320_7f1215 Fixtures ────────────────────────────────

GOLDEN_FULL_TEXT = (
    "ကြက်သွန်နီခွက်နဲ့ ခေါက်ဆွဲပါ။ "
    "ပထမဆုံး တစ်လုတ်ကိုတော့ မင်းတို့ကို အရင် ကျွေးလိုက်မယ်။ "
    "ကြက်ဥလေးလည်း ပါတာ ပေါ့။ "
    "ဒီမှာ ကြည့်လိုက်ပါဦး။ "
    "ကြည့်ရတာ အရမ်း သားနားနေတာပဲ။ "
    "ခေါက်ဆွဲကို မြည်း လိုက်ပါတယ်။ "
    "ကျွန်တော့်မှာ ကြက်ဥပါသေးတာကို မင်းတို့ မေ့နေ ကြပြီထင်တယ်။ "
    "ကြည့်ပါ။ "
    "တွေ့ရဲ့လား။ "
    "အာလူးရော ကြက်ဥပါ ကျွန်တော် ထပ်ပြီး ယူလိုက်ပါ သေးတယ်။ "
    "ကြည့်ရတာ တကယ် စားချင်စရာပါ။ "
    "ခေါက်ဆွဲနဲ့ ကြက်ဥကို တွဲပြီး စားလိုက်တယ်။ "
    "ဒါတွေကို အပြည့် သွတ်ပြီး မြိန်မြိန်ရှက်ရှက် စားပြတော့မှာပါ။"
)

# 23 TTS chunks (arbitrary mid-phrase and cross-sentence breaks as in production)
GOLDEN_23_CHUNKS: list[str] = [
    "ကြက်သွန်နီခွက်နဲ့",
    "ခေါက်ဆွဲပါ။ ပထမဆုံး",
    "တစ်လုတ်ကိုတော့ မင်းတို့ကို",
    "အရင် ကျွေးလိုက်မယ်။",
    "ကြက်ဥလေးလည်း ပါတာ",
    "ပေါ့။ ဒီမှာ ကြည့်လိုက်ပါဦး။",
    "ကြည့်ရတာ အရမ်း",
    "သားနားနေတာပဲ။ ခေါက်ဆွဲကို",
    "မြည်း လိုက်ပါတယ်။",
    "ကျွန်တော့်မှာ ကြက်ဥပါသေးတာကို",
    "မင်းတို့ မေ့နေ",
    "ကြပြီထင်တယ်။ ကြည့်ပါ။",
    "တွေ့ရဲ့လား။ အာလူးရော",
    "ကြက်ဥပါ ကျွန်တော်",
    "ထပ်ပြီး ယူလိုက်ပါ",
    "သေးတယ်။ ကြည့်ရတာ",
    "တကယ် စားချင်စရာပါ။",
    "ခေါက်ဆွဲနဲ့ ကြက်ဥကို",
    "တွဲပြီး စားလိုက်တယ်။",
    "ဒါတွေကို အပြည့်",
    "သွတ်ပြီး",
    "မြိန်မြိန်ရှက်ရှက်",
    "စားပြတော့မှာပါ။",
]

# Owner-specified 16 golden cues (with display punctuation ၊/။ stripped)
GOLDEN_EXPECTED_16_CUES: list[str] = [
    "ကြက်သွန်နီခွက်နဲ့ ခေါက်ဆွဲပါ",
    "ပထမဆုံး တစ်လုတ်ကိုတော့",
    "မင်းတို့ကို အရင် ကျွေးလိုက်မယ်",
    "ကြက်ဥလေးလည်း ပါတာ ပေါ့",
    "ဒီမှာ ကြည့်လိုက်ပါဦး",
    "ကြည့်ရတာ အရမ်း သားနားနေတာပဲ",
    "ခေါက်ဆွဲကို မြည်း လိုက်ပါတယ်",
    "ကျွန်တော့်မှာ ကြက်ဥပါသေးတာကို",
    "မင်းတို့ မေ့နေ ကြပြီထင်တယ်",
    "ကြည့်ပါ တွေ့ရဲ့လား",
    "အာလူးရော ကြက်ဥပါ",
    "ကျွန်တော် ထပ်ပြီး ယူလိုက်ပါ သေးတယ်",
    "ကြည့်ရတာ တကယ် စားချင်စရာပါ",
    "ခေါက်ဆွဲနဲ့ ကြက်ဥကို တွဲပြီး စားလိုက်တယ်",
    "ဒါတွေကို အပြည့် သွတ်ပြီး",
    "မြိန်မြိန်ရှက်ရှက် စားပြတော့မှာပါ",
]


def _build_contiguous_segments(chunks: list[str]) -> tuple[list[dict[str, Any]], float]:
    segments: list[dict[str, Any]] = []
    cursor = 0.0
    for idx, text in enumerate(chunks, 1):
        dur = round(max(0.55, 0.09 * core.grapheme_count(text)), 3)
        end = round(cursor + dur, 3)
        segments.append({
            "segment_id": f"seg_{idx:03d}",
            "source_index": idx,
            "audio_start": round(cursor, 3),
            "audio_end": end,
            "speech_duration": dur,
            "text": text,
        })
        cursor = end
    return segments, cursor


def _subtitle_render_params(font_size: int = 16) -> tuple[int, int, Path, int]:
    layout = core.frame_layout("9:16", "blur", 1080, 1920, crop_top=0.12, crop_bottom=0.12)
    cw, ch, pw = int(layout["cw"]), int(layout["ch"]), int(layout["pw"])
    overlays = {
        "sub_lines": 1,
        "subtitles_font_size": font_size,
        "sub_style": "cinematic",
        "sub_w": 0.92,
        "sub_font": "padauk",
        "sub_bold": True,
    }
    font_px = core.subtitle_font_px(cw, ch, font_size, 1, layout)
    font_path = core.overlays_font_path(overlays)
    vis = core.build_subtitle_visuals(core.SUBTITLE_STYLE_PRESETS["cinematic"], {}, ch, font_px)
    stroke_px = int(vis.get("stroke_px", 1))
    fit_max_w = core._subtitle_fit_width_px(overlays, pw)
    pad_2 = 2 * _png_subs.png_pad_x(vis, font_px, stroke_px)
    max_ink_w = max(80, fit_max_w - pad_2)
    return max_ink_w, font_px, font_path, stroke_px


def _write_test_wav(path: Path, duration_sec: float, freq: float = 330.0, rate: int = 24000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = max(1, int(round(duration_sec * rate)))
    buf = bytearray()
    for i in range(n_samples):
        t = i / float(rate)
        env = 0.0 if (t < 0.04 or t > max(0.05, duration_sec - 0.04)) else 0.35
        sample = int(32767 * env * math.sin(2.0 * math.pi * freq * t))
        buf.extend(int(sample).to_bytes(2, byteorder="little", signed=True))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(bytes(buf))


# ── 1. Golden Test: Job rc_1795337320_7f1215 ────────────────────────────────


@pytest.mark.parametrize("font_size", [16, 13])
def test_golden_rc_1795337320_7f1215_exact_16_phrase_clause_cues(font_size: int):
    assert len(GOLDEN_23_CHUNKS) == 23
    assert " ".join(GOLDEN_23_CHUNKS) == GOLDEN_FULL_TEXT

    segments, total_dur = _build_contiguous_segments(GOLDEN_23_CHUNKS)
    max_ink_w, font_px, font_path, stroke_px = _subtitle_render_params(font_size)

    raw_cues, speech_ends = core.pack_subtitle_cues_from_segments(
        segments,
        lines_per_cue=1,
        max_width_px=max_ink_w,
        font_px=font_px,
        font_path=font_path,
        stroke_px=stroke_px,
    )
    cues = core.apply_reading_speed_rules(
        raw_cues,
        speech_ends=speech_ends,
        timeline_end=total_dur,
    )

    assert [txt for _, _, txt in cues] == GOLDEN_EXPECTED_16_CUES

    # Loose timing invariants: monotone, non-overlapping, inside narration span.
    assert cues[0][0] == pytest.approx(0.0, abs=0.01)
    assert cues[-1][1] <= total_dur + core.SUBTITLE_OVERHANG_SEC + 0.01
    for i, (st, en, txt) in enumerate(cues):
        assert st < en
        if i + 1 < len(cues):
            assert en <= cues[i + 1][0] + 1e-6
        assert "။" not in txt and "၊" not in txt
        assert "  " not in txt
        assert core._measure_text_width_px(txt, font_px, font_path, stroke_px) <= max_ink_w

    # Chunk-boundary invariance: passing the full narration as 1 segment produces the exact same cue texts.
    single_cues, _ = core.pack_subtitle_cues_from_segments(
        [{"audio_start": 0.0, "audio_end": total_dur, "speech_duration": total_dur, "text": GOLDEN_FULL_TEXT}],
        lines_per_cue=1,
        max_width_px=max_ink_w,
        font_px=font_px,
        font_path=font_path,
        stroke_px=stroke_px,
    )
    assert [txt for _, _, txt in single_cues] == GOLDEN_EXPECTED_16_CUES

    # Prewarm streaming packer parity: streaming add_segment/flush matches batch output identically.
    packer = core.SubtitleCuePacker(
        lines_per_cue=1,
        max_width_px=max_ink_w,
        font_px=font_px,
        font_path=font_path,
        stroke_px=stroke_px,
    )
    streamed = []
    for seg in segments:
        streamed.extend(
            packer.add_segment(
                seg["audio_start"],
                seg["audio_end"],
                seg["text"],
                speech_end=seg["audio_start"] + seg["speech_duration"],
            )
        )
    streamed.extend(packer.flush())
    assert [(st, en, txt) for st, en, txt, _ in streamed] == raw_cues


def test_golden_sentence_2_word_timings_place_intra_chunk_clause_boundary():
    """Verify word_timings (both job-absolute and segment-relative) drive intra-chunk
    clause split timing when a sentence splits at its natural clause boundary."""
    max_ink_w, font_px, font_path, stroke_px = _subtitle_render_params(16)
    s2 = "ပထမဆုံး တစ်လုတ်ကိုတော့ မင်းတို့ကို အရင် ကျွေးလိုက်မယ်။"
    # Skewed timings: clause 1 spoken quickly [5.0..6.2], clause 2 spoken over [6.2..10.0]
    abs_wt = [
        {"start": 5.0, "end": 5.5, "text": "ပထမဆုံး"},
        {"start": 5.5, "end": 6.2, "text": "တစ်လုတ်ကိုတော့"},
        {"start": 6.2, "end": 7.5, "text": "မင်းတို့ကို"},
        {"start": 7.5, "end": 8.5, "text": "အရင်"},
        {"start": 8.5, "end": 10.0, "text": "ကျွေးလိုက်မယ်။"},
    ]
    rel_wt = [
        {"start": round(ev["start"] - 5.0, 3), "end": round(ev["end"] - 5.0, 3), "text": ev["text"]}
        for ev in abs_wt
    ]
    cues_abs, _ = core.pack_subtitle_cues_from_segments(
        [{"audio_start": 5.0, "audio_end": 10.0, "speech_duration": 5.0, "text": s2, "word_timings": abs_wt}],
        lines_per_cue=1,
        max_width_px=max_ink_w,
        font_px=font_px,
        font_path=font_path,
        stroke_px=stroke_px,
    )
    cues_rel, _ = core.pack_subtitle_cues_from_segments(
        [{"audio_start": 5.0, "audio_end": 10.0, "speech_duration": 5.0, "text": s2, "word_timings": rel_wt}],
        lines_per_cue=1,
        max_width_px=max_ink_w,
        font_px=font_px,
        font_path=font_path,
        stroke_px=stroke_px,
    )
    expected_texts = ["ပထမဆုံး တစ်လုတ်ကိုတော့", "မင်းတို့ကို အရင် ကျွေးလိုက်မယ်"]
    assert [c[2] for c in cues_abs] == expected_texts
    assert [c[2] for c in cues_rel] == expected_texts
    assert cues_abs[0][1] == pytest.approx(6.2, abs=0.2)
    assert cues_rel[0][1] == pytest.approx(6.2, abs=0.2)


# ── 2. Display Strip vs Narration Script Preservation ───────────────────────


def test_burmese_punctuation_stripped_in_srt_ass_burn_but_preserved_in_script(tmp_path, monkeypatch):
    """Ensure ၊ and ။ never appear in SRT, ASS, or burn cues (and double spaces collapse),
    while the script TXT file and narration plan keep ၊ and ။ intact for readers/TTS."""
    ffmpeg_bin = recap_pipeline.find_binary("ffmpeg")
    source_mp4 = tmp_path / "source.mp4"
    subprocess.run(
        [
            ffmpeg_bin, "-y",
            "-f", "lavfi", "-i", "testsrc=size=540x960:rate=24:duration=10",
            "-f", "lavfi", "-i", "sine=frequency=220:duration=10",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(source_mp4),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    plan_segments = [
        {
            "segment_id": "seg_001",
            "source_index": 1,
            "video_start": 0.0,
            "video_end": 2.5,
            "narration": "မိုးရွာနေတယ်၊  အိမ်ထဲမှာ နေရတာ ပျော်စရာကောင်းတယ်။",
        },
        {
            "segment_id": "seg_002",
            "source_index": 2,
            "video_start": 2.5,
            "video_end": 5.5,
            "narration": "ကြည့်ပါ။တွေ့ရဲ့လား။ အာလူးရော ကြက်ဥပါ ကျွန်တော် ထပ်ပြီး ယူလိုက်ပါ သေးတယ်။",
        },
    ]

    def _fake_planner_build(self, *, source, job_dir, source_title="", reuse=True, **kwargs):
        out_p = Path(job_dir) / "smart_sync_plan.json"
        plan_obj = {
            "title": "Punctuation Parity Test",
            "story_style": "pov",
            "segments": plan_segments,
            "api_usage": {"requests": 1, "input_tokens": 50, "output_tokens": 40, "model": "gemini-2.5-flash"},
            "stage_timings": {"stt": 0.1, "plan": 0.1},
        }
        out_p.write_text(json.dumps(plan_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        return out_p, plan_obj

    monkeypatch.setattr(recap_pipeline.RecapPlanBuilder, "build", _fake_planner_build)
    monkeypatch.setattr("app.services.curated_voices.get_curated_voice", lambda _slug: None)

    def _fake_edge_generate(self, text: str, voice_name: str | None = None) -> Path:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        out_wav = self.work_dir / f"tts_{abs(hash(text)) & 0xFFFFFFFF:08x}.wav"
        dur = 2.0
        _write_test_wav(out_wav, dur)
        words = [w for w in text.split(" ") if w] or [text]
        step = dur / len(words)
        self._tls.last_word_timings = [
            {"start": round(i * step, 3), "end": round((i + 1) * step, 3), "text": w}
            for i, w in enumerate(words)
        ]
        return out_wav

    monkeypatch.setattr(core.EdgeTTSEngine, "generate_channel99_voice", _fake_edge_generate)

    jid = "rc_punct_strip_test_01"
    outer_job_dir = tmp_path / "recap_output" / jid
    workspace_dir = outer_job_dir / "workspace"
    res = recap_pipeline.run_recap_pipeline(
        source_video_path=source_mp4,
        job_dir=workspace_dir,
        voice="my-MM-ThihaNeural",
        tts_engine="edge_tts",
        output_ratio="9:16",
        frame_fill="blur",
        burn_subtitles=True,
        sub_lines=1,
        sub_font_size=16,
        sub_style="cinematic",
        sub_font="padauk",
        sub_w=0.92,
    )

    srt_path = Path(res["subtitles_srt"])
    ass_path = Path(res["subtitles_ass"])
    plan_edge_path = workspace_dir / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json"
    plan_edge = json.loads(plan_edge_path.read_text(encoding="utf-8"))

    srt_cues = _png_subs.parse_srt_cues(srt_path)
    parsed_ass = _png_subs.parse_ass_source(ass_path)
    burned_cues = plan_edge["production_overlays"]["subtitle_cues"]
    png_job = core._resolve_png_subtitle_job(
        plan_edge["production_overlays"], 1080, 1920, {"pw": 1080, "ph": 1460, "ox": 0, "oy": 230}
    )

    assert srt_cues and parsed_ass and burned_cues and png_job
    assert [c[2] for c in burned_cues] == [c[2] for c in srt_cues]
    assert [c[2] for c in parsed_ass["cues"]] == [c[2] for c in srt_cues]
    assert [c[2] for c in png_job["cues"]] == [c[2] for c in srt_cues]

    for _, _, txt in srt_cues:
        assert "။" not in txt
        assert "၊" not in txt
        assert "  " not in txt

    # Combined no-space short sentences "ကြည့်ပါ။တွေ့ရဲ့လား။" must become "ကြည့်ပါ တွေ့ရဲ့လား", never fused.
    assert any("ကြည့်ပါ တွေ့ရဲ့လား" == txt for _, _, txt in srt_cues)

    # Script TXT and plan segments retain ၊ and ။ untouched.
    script_path = api_mod._build_clean_recap_script_file(outer_job_dir, jid, "pov", str(srt_path))
    assert script_path is not None
    script_text = Path(script_path).read_text(encoding="utf-8")
    assert "။" in script_text and "၊" in script_text
    assert "မိုးရွာနေတယ်၊" in script_text
    assert all("။" in seg["text"] for seg in plan_edge["tts_timing_segments"])


# ── 3. Linguistic Segmentation Invariants ───────────────────────────────────


def test_never_glues_across_sentence_boundary_mid_phrase():
    max_ink_w, font_px, font_path, stroke_px = _subtitle_render_params(16)
    segs = [
        {"audio_start": 0.0, "audio_end": 1.0, "speech_duration": 1.0, "text": "ကြက်သွန်နီခွက်နဲ့"},
        {"audio_start": 1.0, "audio_end": 2.2, "speech_duration": 1.2, "text": "ခေါက်ဆွဲပါ။ ပထမဆုံး"},
        {"audio_start": 2.2, "audio_end": 3.4, "speech_duration": 1.2, "text": "တစ်လုတ်ကိုတော့ မင်းတို့ကို"},
        {"audio_start": 3.4, "audio_end": 4.8, "speech_duration": 1.4, "text": "အရင် ကျွေးလိုက်မယ်။"},
    ]
    cues, _ = core.pack_subtitle_cues_from_segments(
        segs,
        lines_per_cue=1,
        max_width_px=max_ink_w,
        font_px=font_px,
        font_path=font_path,
        stroke_px=stroke_px,
    )
    assert [c[2] for c in cues] == [
        "ကြက်သွန်နီခွက်နဲ့ ခေါက်ဆွဲပါ",
        "ပထမဆုံး တစ်လုတ်ကိုတော့",
        "မင်းတို့ကို အရင် ကျွေးလိုက်မယ်",
    ]


def test_only_very_short_complete_sentences_merge_never_short_plus_normal():
    max_ink_w, font_px, font_path, stroke_px = _subtitle_render_params(16)
    text = "ဒီမှာ ကြည့်လိုက်ပါဦး။ ဟုတ်တယ်။ ကြည့်ရတာ အရမ်း သားနားနေတာပဲ။"
    cues, _ = core.pack_subtitle_cues_from_segments(
        [{"audio_start": 0.0, "audio_end": 4.5, "speech_duration": 4.5, "text": text}],
        lines_per_cue=1,
        max_width_px=max_ink_w,
        font_px=font_px,
        font_path=font_path,
        stroke_px=stroke_px,
    )
    assert [c[2] for c in cues] == [
        "ဒီမှာ ကြည့်လိုက်ပါဦး",
        "ဟုတ်တယ်",
        "ကြည့်ရတာ အရမ်း သားနားနေတာပဲ",
    ]


def test_explicit_burmese_comma_splits_overflowing_sentence_and_multiline_respects_sentences():
    max_ink_w, font_px, font_path, stroke_px = _subtitle_render_params(16)
    comma_text = "မိုးရွာနေတယ်၊ အိမ်ထဲမှာ နေရတာ ပျော်စရာကောင်းတယ်။"
    cues_narrow, _ = core.pack_subtitle_cues_from_segments(
        [{"audio_start": 0.0, "audio_end": 4.0, "speech_duration": 4.0, "text": comma_text}],
        lines_per_cue=1,
        max_width_px=700,
        font_px=font_px,
        font_path=font_path,
        stroke_px=stroke_px,
    )
    assert [c[2] for c in cues_narrow] == [
        "မိုးရွာနေတယ်",
        "အိမ်ထဲမှာ နေရတာ",
        "ပျော်စရာကောင်းတယ်",
    ]

    two_sent = "ပထမဆုံး တစ်လုတ်ကိုတော့ မင်းတို့ကို အရင် ကျွေးလိုက်မယ်။ ကြက်ဥလေးလည်း ပါတာ ပေါ့။"
    cues_2l, _ = core.pack_subtitle_cues_from_segments(
        [{"audio_start": 0.0, "audio_end": 5.0, "speech_duration": 5.0, "text": two_sent}],
        lines_per_cue=2,
        max_width_px=max_ink_w,
        font_px=font_px,
        font_path=font_path,
        stroke_px=stroke_px,
    )
    assert [c[2] for c in cues_2l] == [
        "ပထမဆုံး တစ်လုတ်ကိုတော့\nမင်းတို့ကို အရင် ကျွေးလိုက်မယ်",
        "ကြက်ဥလေးလည်း ပါတာ ပေါ့",
    ]


def test_pause_gt_035s_still_blocks_merging_even_for_short_sentences():
    max_ink_w, font_px, font_path, stroke_px = _subtitle_render_params(16)
    segs = [
        {"audio_start": 0.0, "audio_end": 0.9, "speech_duration": 0.9, "text": "ကြည့်ပါ။"},
        {"audio_start": 1.4, "audio_end": 2.3, "speech_duration": 0.9, "text": "တွေ့ရဲ့လား။"},
    ]
    cues, _ = core.pack_subtitle_cues_from_segments(
        segs,
        lines_per_cue=1,
        max_width_px=max_ink_w,
        font_px=font_px,
        font_path=font_path,
        stroke_px=stroke_px,
    )
    assert [c[2] for c in cues] == ["ကြည့်ပါ", "တွေ့ရဲ့လား"]
    assert cues[0][1] <= 0.91
    assert cues[1][0] >= 1.39
