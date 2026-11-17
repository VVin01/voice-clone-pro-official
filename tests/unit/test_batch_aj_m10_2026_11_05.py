"""Batch AJ / M10 — recap slots must not be pinnable for hours.

(a) The yt-dlp media stage gets a TOTAL wall-clock budget: a shared
    progress hook, armed once per logical download and carried by
    every client attempt of that download, raises once the budget is
    spent — a slow-drip host can no longer hold a download (and the
    recap slot behind it) indefinitely.
(b) video_splitter.audio_levels callers passed
    timeout=ffmpeg_timeout(duration) — 18,000 s for a 600 s source.
    The split watchdog is capped (helper at the call sites + a clamp
    inside audio_levels itself).
"""
from __future__ import annotations

import sys
import time

import pytest

from app.services import downloader as dl_mod
from app.services.downloader import MediaDownloader
from app.services.recap_engine import video_splitter as vs

YOUTUBE_URL = "https://www.youtube.com/watch?v=abc12345678"


# ── (a) media download total budget ────────────────────────────────

def test_media_deadline_hook_raises_once_budget_spent():
    hook = dl_mod._media_deadline_progress_hook(
        started=1000.0, budget_seconds=60, clock=lambda: 1000.0 + 30
    )
    hook({"status": "downloading"})  # inside budget: no raise
    late = dl_mod._media_deadline_progress_hook(
        started=1000.0, budget_seconds=60, clock=lambda: 1000.0 + 61
    )
    with pytest.raises(dl_mod.MediaDownloadTimeoutError):
        late({"status": "downloading"})


def test_media_plans_share_one_deadline_hook_and_info_plans_have_none(tmp_path):
    dl = MediaDownloader(download_dir=str(tmp_path))
    base = dl._get_base_opts(YOUTUBE_URL)

    media_plans = list(dl._attempt_plans(base, YOUTUBE_URL, stage="media"))
    assert media_plans, "expected at least one media attempt"
    hooks = []
    for _label, opts in media_plans:
        assert opts.get("progress_hooks"), "media attempt carries no deadline hook"
        hooks.append(opts["progress_hooks"][0])
    # ONE shared deadline for the whole logical download: client
    # rotation must not reset the clock.
    assert all(h is hooks[0] for h in hooks)
    assert dl_mod.MEDIA_DOWNLOAD_TOTAL_TIMEOUT_SECONDS > 0
    assert dl_mod.MEDIA_DOWNLOAD_TOTAL_TIMEOUT_SECONDS <= 3600

    info_plans = list(dl._attempt_plans(base, YOUTUBE_URL, stage="info"))
    for _label, opts in info_plans:
        assert not opts.get("progress_hooks")


def test_download_video_aborts_when_media_budget_already_spent(monkeypatch, tmp_path):
    monkeypatch.setattr(dl_mod, "MEDIA_DOWNLOAD_TOTAL_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(dl_mod, "unshorten_url", lambda u: u)

    class _DripYDL:
        calls = 0

        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            type(self).calls += 1
            # A slow-drip host keeps delivering progress events; the
            # shared deadline hook must abort the download.
            for hook in self.opts.get("progress_hooks", []):
                hook({"status": "downloading", "downloaded_bytes": 1024})
            raise AssertionError("deadline hook did not abort the download")

    monkeypatch.setattr(dl_mod.yt_dlp, "YoutubeDL", _DripYDL)
    dl = MediaDownloader(download_dir=str(tmp_path))
    with pytest.raises(dl_mod.MediaDownloadTimeoutError):
        dl.download_video(YOUTUBE_URL, target_dir=str(tmp_path))
    # Bounded: the 4-client chain plus the one format fallback.
    assert _DripYDL.calls <= 5


# ── (b) split watchdog cap ─────────────────────────────────────────

def test_split_audio_levels_timeout_is_capped():
    # ffmpeg_timeout(600) is 18,000 s — the cap must cut it down.
    assert vs.split_audio_levels_timeout(600) <= 1800
    assert vs.split_audio_levels_timeout(600) == vs.AUDIO_LEVELS_MAX_TIMEOUT_SECONDS
    # Short media keeps the ordinary ffmpeg_timeout floor.
    assert vs.split_audio_levels_timeout(10) == 600
    assert vs.AUDIO_LEVELS_MAX_TIMEOUT_SECONDS <= 1800


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script mock requires Linux")
def test_audio_levels_clamps_an_oversized_caller_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(vs, "AUDIO_LEVELS_MAX_TIMEOUT_SECONDS", 0.5)
    fake_ffmpeg = tmp_path / "ffmpeg"
    fake_ffmpeg.write_text("#!/bin/sh\nexec sleep 30\n")
    fake_ffmpeg.chmod(0o755)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        # A caller still passing the old 5-hour watchdog must be
        # clamped to the cap.
        vs.audio_levels(str(fake_ffmpeg), "whatever.mp4", timeout=18000)
    assert time.monotonic() - started < 10
