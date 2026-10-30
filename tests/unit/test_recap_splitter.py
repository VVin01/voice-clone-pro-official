"""Tests for the production-grade Video Splitter (Feature 1).

Covers the pure logic (labels, cut planning over 50 ms dBFS level
frames) without ffmpeg, plus one end-to-end split of a tiny synthetic
video when ffmpeg is available.
"""
import shutil
import subprocess

import numpy as np
import pytest

from app.services.recap_engine.video_splitter import (
    LEVEL_FRAME,
    part_label,
    plan_parts,
    speech_level,
    split_rendered_video,
)


# ── part_label ──────────────────────────────────────────────────────────────

def test_part_label_myanmar_digits():
    assert part_label("အပိုင်း {mm}/{total}", 2, 3) == "အပိုင်း ၂/၃"
    assert part_label("အပိုင်း {mm}", 12, 12) == "အပိုင်း ၁၂"
    # The module default template:
    assert part_label("အပိုင်း {mm}/{total}", 1, 2) == "အပိုင်း ၁/၂"


def test_part_label_plain_numbers():
    assert part_label("Part {n} of {total}", 1, 4) == "Part 1 of 4"


def test_part_label_no_placeholders_passthrough():
    assert part_label("အပိုင်း", 1, 2) == "အပိုင်း"


# ── plan_parts (levels are dBFS per 50 ms frame) ────────────────────────────

def _levels(duration_s, speech_db=-12.0):
    return np.full(int(duration_s / LEVEL_FRAME) + 2, speech_db, dtype=np.float64)


def test_plan_parts_exact_cuts_when_not_smart():
    parts = plan_parts(90.0, 30.0, smart=False)
    assert [(p["start"], p["end"]) for p in parts] == [
        (0.0, 30.0), (30.0, 60.0), (60.0, 90.0),
    ]
    assert [p["n"] for p in parts] == [1, 2, 3]
    assert parts[0]["cut"] == "exact" and parts[-1]["cut"] == "end"


def test_plan_parts_smart_cut_waits_for_pause():
    lv = _levels(90)
    # Short breaths (-40 dB, 0.25 s) every 2 s — frequent enough that the
    # window's 10th-percentile "floor" sits at breath level, as in real
    # narration — and one genuine pause (-70 dB) at 30.6–31.8 s, just
    # after the 30 s target.
    for t in np.arange(2.0, 90.0, 2.0):
        i = int(t / LEVEL_FRAME)
        lv[i:i + 5] = -40.0
    i0, i1 = int(30.6 / LEVEL_FRAME), int(31.8 / LEVEL_FRAME)
    lv[i0:i1] = -70.0
    parts = plan_parts(90.0, 30.0, smart=True, levels=lv)
    first_end = parts[0]["end"]
    assert 30.6 <= first_end <= 31.8, f"smart cut should land in the pause, got {first_end}"
    assert parts[0]["cut"] == "pause"
    # Parts stay contiguous and cover the whole video.
    assert parts[0]["start"] == 0.0 and parts[-1]["end"] == 90.0
    for a, b in zip(parts, parts[1:]):
        assert a["end"] == b["start"]


def test_plan_parts_merges_short_tail():
    parts = plan_parts(66.0, 30.0, smart=False)
    assert [(p["start"], p["end"]) for p in parts] == [(0.0, 30.0), (30.0, 66.0)]


def test_plan_parts_too_short_single_part():
    parts = plan_parts(20.0, 30.0, smart=False)
    assert len(parts) == 1
    assert parts[0]["start"] == 0.0 and parts[0]["end"] == 20.0


def test_speech_level_percentile():
    lv = np.array([-20.0] * 95 + [-5.0] * 5)
    assert speech_level(lv) == pytest.approx(-20.0)
    silent = np.array([-90.0] * 10)
    assert speech_level(silent) == pytest.approx(-20.0)  # documented fallback


# ── end-to-end (needs ffmpeg) ───────────────────────────────────────────────

def _make_video(path, seconds):
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size=320x180:rate=15",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(path),
        ],
        check=True,
    )


def _duration(path):
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(probe.stdout.strip())


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_split_rendered_video_end_to_end(tmp_path):
    src = tmp_path / "tiny.mp4"
    _make_video(src, 70)
    out_dir = tmp_path / "parts"
    # part_minutes=0.5 -> 30 s parts (the module's 30 s floor).
    parts = split_rendered_video(str(src), out_dir, part_minutes=0.5, smart_cut=False)
    real = [p for p in parts if p["path"]]
    assert len(real) == 3  # 0-30, 30-60, 60-70 (10 s tail kept: > part/4)
    for p in real:
        assert p["path"].endswith(f"recap_part_0{p['n']}.mp4")
    assert 28.0 <= _duration(real[0]["path"]) <= 32.0
    assert 8.0 <= _duration(real[2]["path"]) <= 12.0
    assert real[0]["label"] == "အပိုင်း ၁/၃"
    # Resume: a second run reuses the rendered parts.
    again = split_rendered_video(str(src), out_dir, part_minutes=0.5, smart_cut=False)
    assert [p["path"] for p in again if p["path"]] == [p["path"] for p in real]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_split_too_short_video_returns_single_placeholder(tmp_path):
    src = tmp_path / "short.mp4"
    _make_video(src, 4)
    parts = split_rendered_video(str(src), tmp_path / "parts", part_minutes=3.0, smart_cut=False)
    assert len(parts) == 1 and parts[0]["path"] is None


# ── API request-model defaults for the whole programme ─────────────────────

def test_recap_request_new_option_defaults():
    from app.webapp.api import RecapConfigRequest

    req = RecapConfigRequest(url="https://example.com/v.mp4")
    assert req.split_video is False
    assert req.split_minutes == 3
    assert req.source_language == "auto"
    assert req.render_quality == "standard"
    assert req.tts_engine == "voxcpm"
    assert req.gemini_voice is None


def test_recap_request_rejects_bad_option_values():
    from pydantic import ValidationError

    from app.webapp.api import RecapConfigRequest

    with pytest.raises(ValidationError):
        RecapConfigRequest(url="https://x", source_language="klingon")
    with pytest.raises(ValidationError):
        RecapConfigRequest(url="https://x", render_quality="ultra")
    with pytest.raises(ValidationError):
        RecapConfigRequest(url="https://x", tts_engine="elevenlabs")
    with pytest.raises(ValidationError):
        RecapConfigRequest(url="https://x", split_minutes=99)
    # Normalisation: case/whitespace-insensitive, "detect" means auto.
    assert RecapConfigRequest(url="https://x", source_language=" EN ").source_language == "en"
    assert RecapConfigRequest(url="https://x", source_language="detect").source_language == "auto"
