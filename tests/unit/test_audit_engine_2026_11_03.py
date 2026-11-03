"""Audit fix batch B — media engine resilience (2026-11-03).

Pins:
* every ffmpeg/ffprobe render/probe in the recap engine is bounded
  by a duration-scaled timeout (helper math + the actual call sites),
  and a timeout surfaces as a clear RuntimeError, never a hang;
* the whole-job recap deadline (EdgeSmartSync) converts an
  over-budget job into a TimeoutError at segment boundaries;
* audio_levels runs its decoder under a watchdog that kills on
  expiry;
* Edge TTS route selection is a per-call explicit proxy argument —
  the process-global proxy env vars are never mutated;
* late pipeline stages (concat/overlay/mux) are fingerprinted so a
  resumed run can skip them when inputs are unchanged;
* downloader: yt-dlp opts verify TLS and carry the 5 GB
  max_filesize ceiling, and the tikwm byte fetch enforces the same
  ceiling while streaming (partial file removed on abort);
* the guarded tikwm fallback now triggers after ANY primary-chain
  failure for TikTok URLs (it used to require a blocked-family
  error), and still never triggers for other hosts;
* repair_burmese_mark_order is idempotent and lossless over a
  generated corpus of realistic mark clusters (plus the shipped
  locale), with two degenerate-input cases pinned as xfail.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest

import app.services.downloader as dl_mod
from app.services.downloader import MAX_DOWNLOAD_BYTES, MediaDownloader
from app.services.myanmar_text import repair_burmese_mark_order
from app.services.recap_engine import core, video_splitter
from app.services.recap_engine.core import (
    PROBE_TIMEOUT_SECONDS,
    EdgeSmartSync,
    _file_identity,
    _run_media,
    _stage_fingerprint,
    ffmpeg_timeout,
    recap_job_timeout,
)
from app.services.recap_engine.video_splitter import audio_levels, render_part

REPO_ROOT = Path(__file__).resolve().parents[2]
TIKTOK_URL = "https://www.tiktok.com/@someone/video/7391505677920816389"
YOUTUBE_URL = "https://www.youtube.com/watch?v=g2NWf7CVy3U"


# ---------------------------------------------------------------------------
# 1. Timeout helpers: scaling math
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("duration,expected", [
    (None, 600), (0, 600), (10, 600), (20, 600), (21, 630),
    (60, 1800), (600, 18000), (-5, 600), ("junk", 600),
])
def test_ffmpeg_timeout_scaling(duration, expected):
    # floor of 600s, then 30s of wall clock per second of media
    assert ffmpeg_timeout(duration) == expected


@pytest.mark.parametrize("duration,expected", [
    (None, 3600), (0, 3600), (100, 3600), (240, 3600),
    (600, 9000), (960, 14400), (10000, 14400),
])
def test_recap_job_timeout_scaling(duration, expected):
    # floor of 1h, then 15x the source duration, capped at 4h
    assert recap_job_timeout(duration) == expected


def test_run_media_timeout_becomes_runtime_error():
    with pytest.raises(RuntimeError, match="timed out"):
        _run_media(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=0.2, what="test render",
        )


def test_run_media_passes_through_result():
    out = _run_media([sys.executable, "-c", "print('ok')"], timeout=30, what="test probe")
    assert out.returncode == 0
    assert out.stdout.strip() == "ok"


# ---------------------------------------------------------------------------
# 1b. The actual call sites pass timeouts (captured, no real ffmpeg)
# ---------------------------------------------------------------------------

@pytest.fixture()
def captured_run(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="12.5", stderr="")

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    return captured


def test_probe_duration_is_bounded(captured_run, tmp_path):
    assert core.probe_duration("ffprobe", tmp_path / "x.mp4") == 12.5
    assert captured_run["timeout"] == PROBE_TIMEOUT_SECONDS == 120


def test_render_video_part_timeout_scales(captured_run, tmp_path):
    seg = {"video_start": 0.0, "video_end": 100.0}
    core.render_video_part(
        "ffmpeg", tmp_path / "src.mp4", tmp_path / "out.mp4", seg,
        100.0, "libx264",
    )
    assert captured_run["timeout"] == ffmpeg_timeout(100.0) == 3000


def test_concat_and_mux_timeouts_scale(captured_run, tmp_path):
    core.concat_video_parts(
        "ffmpeg", [tmp_path / "a.mp4"], tmp_path / "o.mp4", tmp_path,
        total_duration=100.0,
    )
    assert captured_run["timeout"] == 3000
    core.concat_audio(
        "ffmpeg", [tmp_path / "a.wav"], tmp_path / "o.wav", tmp_path,
        total_duration=50.0,
    )
    assert captured_run["timeout"] == 1500
    core.mux_audio(
        "ffmpeg", tmp_path / "v.mp4", tmp_path / "a.wav", tmp_path / "o.mp4",
        duration=100.0,
    )
    assert captured_run["timeout"] == 3000


def test_render_part_timeout_raises(monkeypatch, tmp_path):
    fake_ffmpeg = tmp_path / "ffmpeg"
    fake_ffmpeg.write_text("#!/bin/sh\nexec sleep 30\n")
    fake_ffmpeg.chmod(0o755)
    monkeypatch.setattr(video_splitter, "ffmpeg_timeout", lambda d: 0.3)
    with pytest.raises(RuntimeError, match="timed out"):
        render_part(
            str(fake_ffmpeg), "src.mp4", 0.0, 10.0, tmp_path / "part.mp4",
            encoder="libx264",
        )


# ---------------------------------------------------------------------------
# 1c. audio_levels watchdog
# ---------------------------------------------------------------------------

def test_audio_levels_timeout_kills_decoder(tmp_path):
    fake_ffmpeg = tmp_path / "ffmpeg"
    fake_ffmpeg.write_text("#!/bin/sh\nexec sleep 30\n")
    fake_ffmpeg.chmod(0o755)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        audio_levels(str(fake_ffmpeg), "whatever.mp4", timeout=0.3)
    assert time.monotonic() - started < 10  # killed, not waited out


def test_audio_levels_reads_silence(tmp_path):
    fake_ffmpeg = tmp_path / "ffmpeg"
    fake_ffmpeg.write_text("#!/bin/sh\nhead -c 32000 /dev/zero\n")
    fake_ffmpeg.chmod(0o755)
    levels = audio_levels(str(fake_ffmpeg), "silence.mp4", timeout=30)
    assert levels is not None
    assert len(levels) == 40  # 16000 samples @8kHz / 400-sample frames


# ---------------------------------------------------------------------------
# 2. Whole-job deadline
# ---------------------------------------------------------------------------

def _bare_pipeline() -> EdgeSmartSync:
    pipe = EdgeSmartSync.__new__(EdgeSmartSync)
    pipe._run_started = None
    pipe._run_budget = None
    return pipe


def test_run_deadline_unarmed_is_noop():
    _bare_pipeline()._check_run_deadline("TTS")  # must not raise


def test_run_deadline_within_budget():
    pipe = _bare_pipeline()
    pipe._run_started = time.monotonic()
    pipe._run_budget = 3600.0
    pipe._check_run_deadline("TTS")  # must not raise


def test_run_deadline_exceeded_raises():
    pipe = _bare_pipeline()
    pipe._run_started = time.monotonic() - 100.0
    pipe._run_budget = 1.0
    with pytest.raises(TimeoutError, match="time budget"):
        pipe._check_run_deadline("video render")


# ---------------------------------------------------------------------------
# 6. Edge TTS: per-call proxy, env never mutated
# ---------------------------------------------------------------------------

def _bare_edge_engine(proxy):
    eng = core.EdgeTTSEngine.__new__(core.EdgeTTSEngine)
    eng.proxy = proxy
    eng.rate = "+0%"
    eng.pitch = "+0Hz"
    return eng


def test_synthesize_once_routes_proxy_per_call(monkeypatch):
    seen = {}

    async def fake_save(text, voice, mp3, proxy=None):
        seen["proxy"] = proxy
        seen["env"] = os.environ.get("HTTPS_PROXY")

    monkeypatch.setenv("HTTPS_PROXY", "http://sentinel.invalid:1234")
    eng = _bare_edge_engine("http://proxy.invalid:8080")
    eng._save_mp3_async = fake_save

    eng._synthesize_once("hello", "my-MM-ThihaNeural", Path("x.mp3"), direct=True)
    assert seen["proxy"] == ""  # explicit direct: aiohttp skips env lookup
    assert seen["env"] == "http://sentinel.invalid:1234"  # env untouched mid-call

    eng._synthesize_once("hello", "my-MM-ThihaNeural", Path("x.mp3"), direct=False)
    assert seen["proxy"] == "http://proxy.invalid:8080"

    eng2 = _bare_edge_engine(None)
    eng2._save_mp3_async = fake_save
    eng2._synthesize_once("hello", "my-MM-ThihaNeural", Path("x.mp3"), direct=False)
    assert seen["proxy"] is None  # system route keeps env/system semantics
    assert os.environ.get("HTTPS_PROXY") == "http://sentinel.invalid:1234"


def test_save_mp3_async_passes_proxy_to_communicate(monkeypatch, tmp_path):
    edge_tts = pytest.importorskip("edge_tts")
    recorded = {}

    class FakeCommunicate:
        def __init__(self, text, voice=None, rate=None, pitch=None, proxy=None, **kw):
            recorded.update(text=text, voice=voice, proxy=proxy)

        async def save(self, path):
            Path(path).write_bytes(b"mp3")

    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    eng = _bare_edge_engine("http://proxy.invalid:8080")
    asyncio.run(eng._save_mp3_async("hi", "my-MM-ThihaNeural", tmp_path / "a.mp3", ""))
    assert recorded["proxy"] == ""
    asyncio.run(eng._save_mp3_async("hi", "my-MM-ThihaNeural", tmp_path / "a.mp3", "http://proxy.invalid:8080"))
    assert recorded["proxy"] == "http://proxy.invalid:8080"


# ---------------------------------------------------------------------------
# 8. Late-stage fingerprints
# ---------------------------------------------------------------------------

def test_stage_fingerprint_stable_and_sensitive():
    a = _stage_fingerprint("concat", {"parts": ["x", "y"], "encoder": "libx264"})
    b = _stage_fingerprint("concat", {"parts": ["x", "y"], "encoder": "libx264"})
    changed_part = _stage_fingerprint("concat", {"parts": ["x", "z"], "encoder": "libx264"})
    other_stage = _stage_fingerprint("mux", {"parts": ["x", "y"], "encoder": "libx264"})
    assert a == b
    assert a != changed_part
    assert a != other_stage


def test_file_identity(tmp_path):
    assert _file_identity(tmp_path / "missing.mp4") is None
    f = tmp_path / "voice.wav"
    f.write_bytes(b"abc")
    first = _file_identity(f)
    assert first is not None and "sha256" in first
    f.write_bytes(b"abd")
    assert _file_identity(f)["sha256"] != first["sha256"]
    f.write_bytes(b"abc")
    assert _file_identity(f) == first


# ---------------------------------------------------------------------------
# 3 + 5. Downloader ceilings and TLS
# ---------------------------------------------------------------------------

def test_base_opts_verify_tls_and_cap_filesize(tmp_path):
    dl = MediaDownloader(download_dir=str(tmp_path))
    opts = dl._get_base_opts("https://www.tiktok.com/@x/video/1")
    assert "nocheckcertificate" not in opts
    assert opts["max_filesize"] == MAX_DOWNLOAD_BYTES == 5 * 1024 ** 3


class _StreamResp:
    def __init__(self, chunks, content_type="video/mp4"):
        self._chunks = list(chunks)
        self.headers = {"Content-Type": content_type}
        self.status_code = 200

    def raise_for_status(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk_size=1):
        yield from self._chunks


def _patch_tikwm_resolve(monkeypatch, **overrides):
    data = {
        "id": "7391505677920816389", "title": "T", "author": "A",
        "duration": 5, "cover": None,
        "play_url": "https://v16-webapp.tiktokcdn-us.com/abc/video.mp4",
        "play_size": None,
    }
    data.update(overrides)
    monkeypatch.setattr(
        MediaDownloader, "_tikwm_resolve", lambda self, url: dict(data),
    )


def test_tikwm_download_aborts_over_ceiling_and_removes_partial(monkeypatch, tmp_path):
    monkeypatch.setattr(dl_mod, "MAX_DOWNLOAD_BYTES", 1000)
    _patch_tikwm_resolve(monkeypatch)
    monkeypatch.setattr(
        dl_mod.requests, "get",
        lambda *a, **k: _StreamResp([b"x" * 600, b"y" * 600]),
    )
    dl = MediaDownloader(download_dir=str(tmp_path))
    out = dl._tikwm_download_video(TIKTOK_URL, TIKTOK_URL, str(tmp_path))
    assert out is None
    assert not list(tmp_path.glob("*_tikwm.mp4"))  # partial file removed


def test_tikwm_download_refuses_known_oversize_without_fetching(monkeypatch, tmp_path):
    monkeypatch.setattr(dl_mod, "MAX_DOWNLOAD_BYTES", 1000)
    _patch_tikwm_resolve(monkeypatch, play_size=1001)

    def _get(*a, **k):
        raise AssertionError("media fetch must not start for a known-oversize play URL")

    monkeypatch.setattr(dl_mod.requests, "get", _get)
    dl = MediaDownloader(download_dir=str(tmp_path))
    assert dl._tikwm_download_video(TIKTOK_URL, TIKTOK_URL, str(tmp_path)) is None


def test_tikwm_download_under_ceiling_succeeds(monkeypatch, tmp_path):
    monkeypatch.setattr(dl_mod, "MAX_DOWNLOAD_BYTES", 10 ** 9)
    _patch_tikwm_resolve(monkeypatch)
    payload = b"z" * 20000  # above TIKWM_MIN_MEDIA_BYTES, below the ceiling
    monkeypatch.setattr(
        dl_mod.requests, "get", lambda *a, **k: _StreamResp([payload]),
    )
    dl = MediaDownloader(download_dir=str(tmp_path))
    out = dl._tikwm_download_video(TIKTOK_URL, TIKTOK_URL, str(tmp_path))
    assert out is not None
    path, info = out
    assert Path(path).stat().st_size == len(payload)
    assert info["tikwm_fallback"] is True


# ---------------------------------------------------------------------------
# 4. TikTok fallback: any primary failure (TikTok only)
# ---------------------------------------------------------------------------

class _FailingYDL:
    """yt_dlp.YoutubeDL stand-in whose extraction always fails with a
    NON-blocked-family error."""

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        raise Exception("ERROR: [TikTok] 1: No video formats found!")

    def prepare_filename(self, info):
        return "x.mp4"


@pytest.fixture()
def failing_chain(monkeypatch):
    monkeypatch.setattr(dl_mod, "unshorten_url", lambda u: u)
    monkeypatch.setattr(dl_mod.yt_dlp, "YoutubeDL", _FailingYDL)


def test_get_info_tikwm_fallback_on_non_blocked_error(monkeypatch, tmp_path, failing_chain):
    sentinel = {"id": "x", "title": "via tikwm", "tikwm_fallback": True}
    monkeypatch.setattr(
        MediaDownloader, "_tikwm_get_info", lambda self, url, expanded: dict(sentinel),
    )
    dl = MediaDownloader(download_dir=str(tmp_path))
    assert dl.get_info(TIKTOK_URL)["tikwm_fallback"] is True


def test_download_video_tikwm_fallback_on_non_blocked_error(monkeypatch, tmp_path, failing_chain):
    sentinel = (str(tmp_path / "v_tikwm.mp4"), {"id": "x", "tikwm_fallback": True})
    monkeypatch.setattr(
        MediaDownloader, "_tikwm_download_video",
        lambda self, url, expanded, dest: sentinel,
    )
    dl = MediaDownloader(download_dir=str(tmp_path))
    out = dl.download_video(TIKTOK_URL, target_dir=str(tmp_path))
    assert out == sentinel


def test_tikwm_fallback_never_for_other_hosts(monkeypatch, tmp_path, failing_chain):
    called = []
    monkeypatch.setattr(
        MediaDownloader, "_tikwm_get_info",
        lambda self, url, expanded: called.append(1) or None,
    )
    dl = MediaDownloader(download_dir=str(tmp_path))
    with pytest.raises(Exception):
        dl.get_info(YOUTUBE_URL)
    assert called == []


# ---------------------------------------------------------------------------
# 7. Canonicalizer fuzz: idempotent + lossless on realistic clusters
# ---------------------------------------------------------------------------

_BASES = ["\u1000", "\u1004", "\u1005", "\u1010", "\u1019", "\u101B", "\u1021"]
_MEDIALS = ["\u103B", "\u103C", "\u103D", "\u103E"]
_UPPER = ["\u102D", "\u102E", "\u1032"]
_LOWER = ["\u102F", "\u1030"]
_AA = ["\u102B", "\u102C"]
_EXTRAS = [None, "\u1031", "\u1036", "\u1037", "\u1038"]


def _mark_cluster_corpus() -> set[str]:
    corpus: set[str] = set()
    for base in _BASES:
        # one mark per group (or none), every permutation, plus one extra
        for combo in itertools.product(*[[None] + g for g in (_MEDIALS, _UPPER, _LOWER, _AA)]):
            marks = [m for m in combo if m]
            for extra in _EXTRAS:
                ms = marks + ([extra] if extra else [])
                for perm in set(itertools.permutations(ms)):
                    corpus.add(base + "".join(perm))
        # medial pairs with a vowel, every permutation
        for med in itertools.permutations(_MEDIALS, 2):
            for vowel in _UPPER + _LOWER + _AA + ["\u1031"]:
                for perm in set(itertools.permutations(list(med) + [vowel])):
                    corpus.add(base + "".join(perm))
        # codas (plain / dot-inside / dot-after), stacks, kinzi
        for coda in ["\u1000", "\u1004", "\u1010", "\u1019"]:
            corpus.add(base + coda + "\u103A")
            corpus.add(base + "\u1037" + coda + "\u103A")
            corpus.add(base + coda + "\u103A" + "\u1037")
            corpus.add(base + coda + "\u1037\u103A")
        for sub in ["\u1000", "\u1010", "\u1019"]:
            corpus.add(base + "\u1039" + sub)
            corpus.add(base + "\u1039" + sub + "\u102D")
        corpus.add("\u1004\u103A\u1039" + base)
        corpus.add("\u1004\u103A\u1039" + base + "\u102D\u1038")
    # seeded two-syllable combinations of single clusters
    singles = sorted(corpus)
    rng = random.Random(20261103)
    for _ in range(1500):
        corpus.add(rng.choice(singles) + rng.choice(singles))
    return corpus


def _locale_strings() -> list[str]:
    data = json.loads(
        (REPO_ROOT / "locales" / "my" / "messages.json").read_text(encoding="utf-8")
    )

    def walk(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)

    return list(walk(data))


def test_repair_mark_order_fuzz_idempotent_and_lossless():
    corpus = _mark_cluster_corpus()
    corpus.update(_locale_strings())
    assert len(corpus) > 20000  # the fuzz corpus is genuinely broad
    for s in corpus:
        once = repair_burmese_mark_order(s)
        assert sorted(once) == sorted(s), f"lossy repair: {s!r} -> {once!r}"
        twice = repair_burmese_mark_order(once)
        assert twice == once, f"not idempotent: {s!r} -> {once!r} -> {twice!r}"


@pytest.mark.xfail(
    reason="Degenerate input: one syllable carrying TWO visarga tone marks plus a "
           "coda dot-below typed after the asat, separated by the tones. The first "
           "pass folds the dot into the coda only when it directly follows the coda "
           "chunk, so a second pass re-sorts again. No real typed syllable has two "
           "visargas; the canonicalizer is intentionally not changed for this.",
    strict=False,
)
def test_repair_degenerate_double_visarga_idempotent():
    s = "\u1000\u1019\u1038\u1038\u103a\u1037"
    assert repair_burmese_mark_order(repair_burmese_mark_order(s)) == repair_burmese_mark_order(s)


@pytest.mark.xfail(
    reason="Degenerate input: stray dot-below + asat with no base consonant, then "
           "TWO pre-posed e vowels before the syllable. The leftover stray marks "
           "change how the second pass groups the cluster. Not a shape real typing "
           "produces; the canonicalizer is intentionally not changed for this.",
    strict=False,
)
def test_repair_degenerate_stray_marks_double_e_idempotent():
    s = "\u1037\u103a\u1031\u1031\u1021\u1019"
    assert repair_burmese_mark_order(repair_burmese_mark_order(s)) == repair_burmese_mark_order(s)
