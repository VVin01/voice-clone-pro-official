"""Batch AJ / L21 — ffmpeg/ffprobe protocol surface (CONFIRMED).

Repro evidence (run locally against the unmodified tree):
* A 36-byte TEXT file named ``evil.mp4`` whose body is an ffconcat
  script referencing a RELATIVE local file (``file 'seg.mp4'``) was
  probed by the tree's own probe_local_media and came back with the
  REFERENCED file's streams (h264/aac, 320x240) — the concat
  demuxer followed the local reference.
* The tree's probe_duration happily probed an http:// URL served by
  a local HTTP server (returned the remote file's 3.0 s duration) —
  no protocol restriction anywhere (grep protocol_whitelist = 0).

Fix: every ffmpeg/ffprobe invocation in the owned files carries
``-protocol_whitelist file,pipe``. Local files, the engine's own
concat demuxer lists (file), lavfi sources and pipe outputs keep
working (verified against the real binaries); network protocols
(http/https/tcp/...) are refused. NOTE: same-directory relative
file references inside a concat script still resolve — the ``file``
protocol itself is required for every legitimate local input, and
ffmpeg's concat safe-mode already blocks absolute / parent paths;
the whitelist closes the network-protocol surface.
"""
from __future__ import annotations

import functools
import http.server
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from app.services import downloader as dl_mod
from app.services.recap_engine import core as core_mod

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available"
)

WL = ["-protocol_whitelist", "file,pipe"]


def _make_video(path: Path) -> Path:
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi",
         "-i", "color=c=red:size=320x240:rate=10:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)],
        check=True, capture_output=True,
    )
    return path


def test_downloader_probe_command_carries_whitelist(monkeypatch, tmp_path):
    video = _make_video(tmp_path / "v.mp4")
    captured = {}
    real_run = dl_mod.subprocess.run

    def spy(cmd, *a, **kw):
        captured["cmd"] = list(cmd)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(dl_mod.subprocess, "run", spy)
    info = dl_mod.probe_local_media(str(video))
    assert info["has_video"] is True and info["duration"] == pytest.approx(3.0, abs=0.2)
    cmd = captured["cmd"]
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "file,pipe"


def test_core_probe_duration_refuses_http_input(tmp_path):
    video = _make_video(tmp_path / "seg.mp4")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/seg.mp4"
        with pytest.raises(RuntimeError):
            core_mod.probe_duration(FFPROBE, url)
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_core_probe_duration_still_reads_local_files(tmp_path):
    video = _make_video(tmp_path / "v.mp4")
    assert core_mod.probe_duration(FFPROBE, video) == pytest.approx(3.0, abs=0.2)


def test_core_run_media_injects_whitelist_for_input_commands(monkeypatch, tmp_path):
    video = _make_video(tmp_path / "v.mp4")
    captured = {}
    real_run = core_mod.subprocess.run

    def spy(cmd, *a, **kw):
        captured["cmd"] = list(cmd)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(core_mod.subprocess, "run", spy)
    core_mod.probe_video_size(FFPROBE, video)
    cmd = captured["cmd"]
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "file,pipe"


def test_engine_concat_of_local_parts_still_works(tmp_path):
    # The engine's OWN concat flow (its generated list file, local
    # parts) must survive the whitelist: nested parts use `file`.
    part = _make_video(tmp_path / "part.mp4")
    out = tmp_path / "joined.mp4"
    core_mod.concat_video_parts(FFMPEG, [part], out, tmp_path, total_duration=3.0)
    assert out.is_file() and out.stat().st_size > 1000
    assert core_mod.probe_duration(FFPROBE, out) == pytest.approx(3.0, abs=0.3)
