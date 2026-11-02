"""Tests for >10-minute recap handling (owner spec 2026-10-30).

Owner's design: the 10-minute per-job cap is intentional and applies
to everyone, VIP included. Over-cap sources go through the SPLIT PATH:
the source is cut at narration pauses into <=cap segments and each
segment is processed on its own; every part is downloadable (Mini App
+ Telegram). The only exemption is ADMIN (settings.is_admin), who may
run a long video as a single job. Credit rules are unchanged.

These tests pin:

* the cap matrix and the submit/worker split decisions (a) regular
  >10 min single job rejected, (b) VIP >10 min single job rejected,
  (c) admin >10 min allowed, (d) the split path produces parts each
  <=10 min and all downloadable,
* a synthetic 12:34 source planning into sane parts at narration
  pauses, an end-to-end ffmpeg split, and source segmentation,
* part resolution (registry + disk) and Telegram part delivery,
* the Mini App contracts (split steering in JS, preview centering +
  blurred backdrop, Myanmar-safe toast typography, header squeeze).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LEVEL_FRAME = 0.05


# ── Duration cap matrix + split decisions ──────────────────────────────

def test_recap_max_duration_allowed_matrix():
    from app.webapp.api import _recap_max_duration_allowed

    # The 10-minute cap applies to everyone; split never raises it.
    assert _recap_max_duration_allowed(False, is_admin=False) == 120
    assert _recap_max_duration_allowed(True, is_admin=False) == 600
    # Admin exemption (settings.is_admin identity): no practical cap.
    assert _recap_max_duration_allowed(True, is_admin=True) > 3600
    assert _recap_max_duration_allowed(False, is_admin=True) > 3600


def test_submit_decision_regular_user_over_10min_rejected():
    """(a) A regular (free) user cannot run a >10 min single job, and
    split does not loosen the free tier cap either."""
    from app.webapp.api import _recap_submit_duration_decision

    base = dict(is_vip=False, is_admin=False, duration_seconds=754.0)
    assert _recap_submit_duration_decision(split_video=False, script_review=False, **base) == "reject"
    assert _recap_submit_duration_decision(split_video=True, script_review=False, **base) == "reject"
    # Under the free cap is fine.
    assert _recap_submit_duration_decision(
        is_vip=False, is_admin=False, split_video=False, script_review=False,
        duration_seconds=100.0,
    ) == "ok"


def test_submit_decision_vip_over_10min_needs_split_path():
    """(b) A VIP >10 min single job is still capped; with split on (and
    no script review) it is accepted for segmented processing."""
    from app.webapp.api import _recap_submit_duration_decision

    base = dict(is_vip=True, is_admin=False, duration_seconds=754.0)
    assert _recap_submit_duration_decision(split_video=False, script_review=False, **base) == "reject"
    assert _recap_submit_duration_decision(split_video=True, script_review=False, **base) == "segment"
    # Script review edits one whole-video script: cannot combine.
    assert _recap_submit_duration_decision(split_video=True, script_review=True, **base) == "reject"
    # Under the cap is a normal single job.
    assert _recap_submit_duration_decision(
        is_vip=True, is_admin=False, split_video=False, script_review=False,
        duration_seconds=500.0,
    ) == "ok"


def test_submit_decision_admin_exempt():
    """(c) An admin may submit a >10 min video as one job, no split."""
    from app.webapp.api import _recap_submit_duration_decision

    assert _recap_submit_duration_decision(
        is_vip=True, is_admin=True, split_video=False, script_review=False,
        duration_seconds=754.0,
    ) == "ok"
    assert _recap_submit_duration_decision(
        is_vip=True, is_admin=True, split_video=False, script_review=True,
        duration_seconds=3600.0,
    ) == "ok"


def test_should_segment_matrix():
    from app.webapp.api import _recap_should_segment

    assert _recap_should_segment(is_vip=True, is_admin=False, split_video=True, duration_seconds=754.0) is True
    assert _recap_should_segment(is_vip=True, is_admin=False, split_video=True, duration_seconds=500.0) is False
    assert _recap_should_segment(is_vip=True, is_admin=False, split_video=False, duration_seconds=754.0) is False
    # Owner ruling 2026-11-02 (admin exempt from ALL limits — a VIP-only
    # feature gate must not exclude admin): admin + split + over the
    # standard cap now segments, exactly like a VIP. This assertion was
    # `is False` before the ruling and was changed DELIBERATELY for it;
    # the full admin matrix is pinned in
    # tests/unit/test_recap_admin_segmentation_2026_11_02.py.
    assert _recap_should_segment(is_vip=True, is_admin=True, split_video=True, duration_seconds=754.0) is True
    assert _recap_should_segment(is_vip=False, is_admin=False, split_video=True, duration_seconds=754.0) is False


# ── Synthetic 12:34 source → part plan ─────────────────────────────────

def _levels(duration_s, speech_db=-12.0):
    return np.full(int(duration_s / LEVEL_FRAME) + 2, speech_db, dtype=np.float64)


def test_plan_parts_for_12_minute_source():
    from app.services.recap_engine.video_splitter import plan_parts

    duration = 754.0  # 12:34 — the owner's audit case
    lv = _levels(duration)
    # Genuine narration pauses (silence) shortly after each 3-min target.
    for start in (183.0, 366.0, 549.0):
        a = int(start / LEVEL_FRAME)
        b = int((start + 1.4) / LEVEL_FRAME)
        lv[a:b] = -70.0

    parts = plan_parts(duration, 180.0, smart=True, levels=lv)
    assert len(parts) >= 4
    assert parts[0]["start"] == 0.0
    assert parts[-1]["end"] == pytest.approx(duration, abs=0.1)
    # Contiguous coverage; full parts stay within target + smart slack,
    # and the tail is never a tiny sliver (MIN_TAIL_SEC joins it instead).
    for prev, cur in zip(parts, parts[1:]):
        assert cur["start"] == pytest.approx(prev["end"], abs=0.1)
    for p in parts[:-1]:
        assert 120.0 <= (p["end"] - p["start"]) <= 200.5
    assert (parts[-1]["end"] - parts[-1]["start"]) >= 15.0


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")
def test_split_rendered_video_over_10_minutes_e2e(tmp_path):
    """A synthetic 12-minute video actually cuts into downloadable parts."""
    from app.services.recap_engine.video_splitter import split_rendered_video
    from app.services.recap_engine.core import probe_duration

    src = tmp_path / "long_source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=black:s=320x180:r=2:d=720",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "720", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src),
        ],
        check=True, timeout=240,
    )
    assert src.stat().st_size > 1000

    parts = split_rendered_video(
        str(src), tmp_path / "parts", part_minutes=3.0, smart_cut=False
    )
    assert len(parts) == 4
    ffprobe = shutil.which("ffprobe")
    for p in parts:
        assert p["path"] and Path(p["path"]).is_file()
        assert Path(p["path"]).stat().st_size > 1000
        dur = probe_duration(ffprobe, Path(p["path"]))
        assert 150.0 <= dur <= 210.0
        assert p["label"]  # Myanmar part label present


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")
def test_split_source_into_segments_each_under_cap(tmp_path):
    """(d) The split path cuts a >10 min source into segments that each
    respect the 10-minute cap (worker target is cap-30s; the smart cut
    may add up to 20s of slack to reach a narration pause)."""
    from app.services.recap_engine.core import probe_duration
    from app.services.recap_engine.video_splitter import split_source_into_segments

    src = tmp_path / "long_source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=black:s=320x180:r=2:d=720",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "720", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src),
        ],
        check=True, timeout=240,
    )
    ffprobe = shutil.which("ffprobe")

    segments = split_source_into_segments(
        str(src), tmp_path / "segments", part_seconds=570.0,
        ffmpeg="ffmpeg", ffprobe=ffprobe,
    )
    assert len(segments) == 2
    for seg in segments:
        assert Path(seg["path"]).is_file()
        dur = probe_duration(ffprobe, Path(seg["path"]))
        assert dur <= 600.0  # every part respects the 10-minute cap
        assert seg["end"] - seg["start"] <= 600.0

    # And with the user's 3-minute part size, more, smaller segments.
    segments3 = split_source_into_segments(
        str(src), tmp_path / "segments3", part_seconds=180.0,
        ffmpeg="ffmpeg", ffprobe=ffprobe,
    )
    assert len(segments3) == 4
    for seg in segments3:
        assert probe_duration(ffprobe, Path(seg["path"])) <= 210.0


def test_segmented_parts_all_resolvable_for_download(tmp_path, monkeypatch):
    """(d) Segment renders placed in the canonical parts layout all
    resolve through the same resolver the Mini App download endpoint
    and the bot delivery use."""
    from app.config import settings as _settings_obj
    from app.webapp.api import (
        RECAP_JOBS_REGISTRY,
        resolve_recap_deliverable_path,
        resolve_recap_part_paths,
    )

    monkeypatch.setattr(_settings_obj, "storage_root", tmp_path)
    job_id = "rc_test_segmented_downloads"
    RECAP_JOBS_REGISTRY[job_id] = {"segmented": True}
    try:
        for n in (1, 2, 3):
            _make_part_file(tmp_path, job_id, n)
        parts = resolve_recap_part_paths(job_id)
        assert [p["n"] for p in parts] == [1, 2, 3]
        for p in parts:
            resolved = resolve_recap_deliverable_path(job_id, f"part{p['n']}")
            assert resolved == p["path"]
    finally:
        RECAP_JOBS_REGISTRY.pop(job_id, None)


# ── Part resolution + Telegram delivery ────────────────────────────────

def _make_part_file(root: Path, job_id: str, n: int, size: int = 4096) -> Path:
    d = root / "recap_output" / job_id / "parts"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"recap_part_{n:02d}.mp4"
    with open(f, "wb") as fh:
        fh.truncate(size)
    return f


def test_resolve_recap_part_paths_from_disk(tmp_path, monkeypatch):
    from app.config import settings as _settings_obj
    from app.webapp.api import RECAP_JOBS_REGISTRY, resolve_recap_part_paths

    monkeypatch.setattr(_settings_obj, "storage_root", tmp_path)
    job_id = "rc_test_disk_parts"
    RECAP_JOBS_REGISTRY.pop(job_id, None)
    _make_part_file(tmp_path, job_id, 2)
    _make_part_file(tmp_path, job_id, 1)
    try:
        parts = resolve_recap_part_paths(job_id)
    finally:
        RECAP_JOBS_REGISTRY.pop(job_id, None)
    assert [p["n"] for p in parts] == [1, 2]
    assert parts[0]["label"] == "အပိုင်း ၁/၂"
    assert parts[1]["label"] == "အပိုင်း ၂/၂"


def test_resolve_recap_part_paths_registry_labels_win(tmp_path, monkeypatch):
    from app.config import settings as _settings_obj
    from app.webapp.api import RECAP_JOBS_REGISTRY, resolve_recap_part_paths

    monkeypatch.setattr(_settings_obj, "storage_root", tmp_path)
    job_id = "rc_test_registry_parts"
    f1 = _make_part_file(tmp_path, job_id, 1)
    RECAP_JOBS_REGISTRY[job_id] = {
        "parts": [{"n": 1, "label": "အပိုင်း ၁/၁ (Custom)"}],
        "part1_path": str(f1),
    }
    try:
        parts = resolve_recap_part_paths(job_id)
    finally:
        RECAP_JOBS_REGISTRY.pop(job_id, None)
    assert len(parts) == 1
    assert parts[0]["label"] == "အပိုင်း ၁/၁ (Custom)"


class _FakeBot:
    def __init__(self):
        self.videos = []
        self.messages = []

    async def send_video(self, chat_id, video, caption=None, parse_mode=None):
        self.videos.append({"chat_id": chat_id, "caption": caption})

    async def send_message(self, chat_id, text, parse_mode=None, **kw):
        self.messages.append({"chat_id": chat_id, "text": text})


def test_deliver_recap_parts_sends_every_part(tmp_path, monkeypatch):
    from app.config import settings as _settings_obj
    from app.webapp.api import RECAP_JOBS_REGISTRY, _deliver_recap_parts

    monkeypatch.setattr(_settings_obj, "storage_root", tmp_path)
    job_id = "rc_test_deliver_parts"
    RECAP_JOBS_REGISTRY.pop(job_id, None)
    _make_part_file(tmp_path, job_id, 1)
    _make_part_file(tmp_path, job_id, 2)
    bot = _FakeBot()
    try:
        sent = asyncio.run(_deliver_recap_parts(bot, 123, job_id, "my"))
    finally:
        RECAP_JOBS_REGISTRY.pop(job_id, None)
    assert sent == 2
    assert len(bot.videos) == 2
    assert "၁/၂" in bot.videos[0]["caption"]
    assert "၂/၂" in bot.videos[1]["caption"]
    assert bot.messages == []


def test_deliver_recap_parts_oversized_part_notes_app(tmp_path, monkeypatch):
    from app.config import settings as _settings_obj
    from app.webapp.api import RECAP_JOBS_REGISTRY, _deliver_recap_parts

    monkeypatch.setattr(_settings_obj, "storage_root", tmp_path)
    job_id = "rc_test_deliver_big_part"
    RECAP_JOBS_REGISTRY.pop(job_id, None)
    _make_part_file(tmp_path, job_id, 1, size=60 * 1024 * 1024)  # sparse >50MB
    bot = _FakeBot()
    try:
        sent = asyncio.run(_deliver_recap_parts(bot, 123, job_id, "en"))
    finally:
        RECAP_JOBS_REGISTRY.pop(job_id, None)
    assert sent == 0
    assert bot.videos == []
    assert len(bot.messages) == 1
    assert "Mini App" in bot.messages[0]["text"]


# ── Locale keys for part delivery (bot i18n parity) ────────────────────

def test_part_delivery_locale_keys_exist_both_languages():
    for lang in ("my", "en"):
        data = json.loads(
            (REPO_ROOT / "locales" / lang / "messages.json").read_text(encoding="utf-8")
        )
        assert "{label}" in data["recap_part_caption"]
        assert "{size_formatted}" in data["recap_part_caption"]
        assert "{label}" in data["recap_part_too_large"]


# ── Mini App contracts ─────────────────────────────────────────────────

def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_miniapp_split_aware_duration_contract():
    app_js = _read("app/webapp/static/js/app.js")
    api_py = _read("app/webapp/api.py")
    # Client steering helpers (cap is never raised client-side either)
    assert "function recapNormalMaxSeconds()" in app_js
    assert "function enableRecapSplitForLongVideo()" in app_js
    assert "recap_split_max_duration_seconds" not in app_js
    assert "recap_split_max_duration_seconds" not in api_py
    # Server: submit + worker decisions and the admin exemption hook
    # (the same settings.is_admin identity /api/user/me uses).
    assert "_recap_submit_duration_decision(" in api_py
    assert "_recap_should_segment(" in api_py
    assert "settings.is_admin(user_id)" in api_py
    assert "split_source_into_segments" in api_py
    assert api_py.count("_recap_max_duration_allowed(") >= 3  # def + submit + worker


def test_miniapp_preview_centering_and_backdrop_contract():
    app_js = _read("app/webapp/static/js/app.js")
    html = _read("app/webapp/static/index.html")
    css = _read("app/webapp/static/css/style.css")
    # Letterbox strip is centered, pillarbox strip is centered
    assert "((100 - effectiveHeight) / 2)" in app_js
    assert "((100 - effectiveWidth) / 2)" in app_js
    # Blurred backdrop element + wiring + styles
    assert 'id="video-backdrop-blur"' in html
    assert "video-backdrop-blur" in app_js
    assert ".video-backdrop-blur" in css
    assert "blur(16px)" in css


def test_miniapp_toast_and_header_typography_contract():
    css = _read("app/webapp/static/css/style.css")
    i18n_js = _read("app/webapp/static/js/i18n.js")
    # Toast gets a real width (not the ≤50vw shrink-fit column)
    assert "width: min(430px, calc(100vw - 32px))" in css
    # Myanmar-safe line-heights scoped by the language body class
    assert "body.lang-my .studio-toast { line-height: 1.95; }" in css
    assert "body.lang-my .floating-alert-desc { line-height: 1.9; }" in css
    assert 'classList.toggle("lang-my"' in i18n_js
    # Header: brand track may shrink so pills never overflow the edge
    assert "grid-template-columns: minmax(0, 1fr) auto" in css
    # Subtitle sample span can shrink so its line-clamp engages
    assert ".preview-sub-box .preview-sub-text { min-width: 0; flex: 1 1 auto; }" in css
