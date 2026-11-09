"""Batch DB — the CS uniform restart must not kill its own renderer.

Production evidence (2026-11-09, paid user 9117134716): the 🔁
uniform restart fired live for the first time (Gemini pool heavily
used, lines fell back en masse) and killed 4 of 4 jobs. The restart
block stopped the pipelined part renderer with join(timeout=30) and
proceeded even though the thread was still rendering (a real job's
render backlog always outlasts 30 s); it then deleted every video
part file — including the one the still-running ffmpeg had open.
When that encode finished, the +faststart pass re-opened its output
by name, found it gone ("Unable to re-open … output file for
shifting data"), and the renderer's stale error, stored in the
SHARED _render_state after the restart had cleared it, failed the
whole job at the final stage.

The fix (core.py _stop_part_renderer): set the abort flag first so
the thread skips its backlog, join until the thread is really dead,
only then wipe parts — and start pass 2 with a fresh _render_state.
"""
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not available"
)


# ── DB-1: source pins — the timed-join stop pattern is gone ─────────

def test_db_restart_stop_waits_for_renderer_death():
    src = (ROOT / "app/services/recap_engine/core.py").read_text(encoding="utf-8")
    # The defective stop pattern (both call sites) is gone.
    assert "_renderer_thread.join(timeout=30)" not in src
    # A single stop helper exists and is used by the restart block
    # and by the TTS-error cleanup path.
    assert src.count("_stop_part_renderer()") >= 2
    helper = src[src.index("def _stop_part_renderer"):src.index("for _tts_pass in range(2)")]
    assert "_render_abort.set()" in helper
    assert "while _renderer_thread.is_alive():" in helper
    assert "_render_abort.clear()" in helper
    # The restart block stops the renderer BEFORE wiping state, and
    # pass 2 gets a fresh error dict.
    restart = src[src.index("restarting narration uniformly"):src.index("if not restart_uniform:")]
    assert restart.index("_stop_part_renderer()") < restart.index('checkpoint["video"] = {}')
    assert '_render_state = {"error": None}' in restart


# ── DB-2: behavioural — a slow in-flight render survives the restart ──

def _write_wav(path: Path, seconds: float = 0.6, freq: int = 660) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}",
         "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(path)],
        check=True,
    )
    return path


class _FakeEdge:
    def __init__(self, **kw):
        self.work_dir = Path(kw.get("work_dir") or ".")

    def generate_channel99_voice(self, text, voice):
        return _write_wav(self.work_dir / f"edge_{abs(hash(text)) % 99999}.wav", freq=440)


class _FakeGemini:
    calls = 0
    fail_on = ""

    def __init__(self, **kw):
        self.work_dir = Path(kw.get("work_dir") or ".")

    def generate_channel99_voice(self, text, voice):
        _FakeGemini.calls += 1
        if _FakeGemini.fail_on and _FakeGemini.fail_on in text:
            raise RuntimeError("fake gemini quota exhausted")
        return _write_wav(self.work_dir / f"gem_{_FakeGemini.calls}.wav", freq=660)


_persona_calls = {"n": 0}


def _setup_job(tmp_path: Path) -> Path:
    job = tmp_path / "job"
    job.mkdir()
    src = job / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=duration=3:size=320x180:rate=30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(src)],
        check=True,
    )
    plan = {
        "segments": [
            {"video_start": 0.0, "video_end": 1.5, "narration": "ပထမ စာကြောင်း ဖြစ်ပါတယ်"},
            {"video_start": 1.5, "video_end": 3.0, "narration": "ဒုတိယ စာကြောင်း ဖြစ်ပါတယ်"},
        ]
    }
    (job / "smart_sync_plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return job


@pytestmark_ffmpeg
def test_db_restart_waits_for_inflight_render_and_discards_its_error(tmp_path, monkeypatch):
    import app.services.curated_voices as cv
    import app.services.recap_engine.core as core
    import app.services.recap_engine.gemini_tts_engine as gem_mod

    monkeypatch.setattr(core, "EdgeTTSEngine", _FakeEdge)
    monkeypatch.setattr(gem_mod, "GeminiTTSEngine", _FakeGemini)
    monkeypatch.setattr(gem_mod, "collect_gemini_keys", lambda: ["dummy-key"])

    def _fake_persona(text, persona_id, output_wav, speed=1.1, log_fn=None):
        _persona_calls["n"] += 1
        return _write_wav(Path(output_wav), freq=550)

    monkeypatch.setattr(cv, "synthesize_curated_persona", _fake_persona)
    _FakeGemini.calls = 0
    _FakeGemini.fail_on = "ဒုတိယ"  # second line falls back → restart fires
    _persona_calls["n"] = 0

    # Shrink every timed join to 0.5 s: this simulates the production
    # condition — a render backlog that far outlasts the join window —
    # without making the test wait out the real 30 s. A stop that
    # only waits one join window (the bug) proceeds while the render
    # is still blocked; a stop that loops until the thread is dead
    # (the fix) stays put.
    real_join = threading.Thread.join

    def _clamped_join(self, timeout=None):
        if timeout is not None and timeout > 0.5:
            timeout = 0.5
        return real_join(self, timeout)

    monkeypatch.setattr(threading.Thread, "join", _clamped_join)

    # The first part render (pass 0, pipelined thread) blocks until
    # the test releases it, then fails exactly the way production
    # did: its output file was wiped mid-encode and the faststart
    # re-open found nothing.
    real_render = core.render_video_part
    render_started = threading.Event()
    release_render = threading.Event()
    render_calls = {"n": 0}

    def _blocking_render(*args, **kwargs):
        render_calls["n"] += 1
        if render_calls["n"] == 1:
            render_started.set()
            release_render.wait(timeout=30)
            raise RuntimeError(
                "Video segment render failed (libx264):\n"
                "[mp4] Unable to re-open …/video_parts/S0001_….mp4 "
                "output file for shifting data"
            )
        return real_render(*args, **kwargs)

    monkeypatch.setattr(core, "render_video_part", _blocking_render)

    job = _setup_job(tmp_path)
    outcome = {}

    def _run_job():
        from app.services.recap_engine.core import EdgeSmartSync

        sync = EdgeSmartSync(
            job_dir=str(job),
            source_path=str(job / "source.mp4"),
            plan_path=str(job / "smart_sync_plan.json"),
            voice="voxcpm_komin",
            output_ratio="Original",
            mirror_video=False,
            zoom_factor=1.0,
            overlays={"tts_engine": "gemini", "gemini_voice": "Charon", "is_female": False},
            log=lambda m: None,
        )
        try:
            outcome["result"] = sync.run()
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    runner = threading.Thread(target=_run_job, daemon=True)
    runner.start()
    try:
        assert render_started.wait(timeout=30), "pass-0 renderer never started"
        # Wait until pass 0's TTS is done (persona spoke the fallen
        # line once) — the restart decision point.
        deadline = time.time() + 30
        while _persona_calls["n"] < 1 and time.time() < deadline:
            time.sleep(0.05)
        assert _persona_calls["n"] == 1
        # While the pass-0 render is still in flight, the restart
        # must NOT proceed: no pass-2 persona calls, job still alive.
        # (The buggy stop proceeded after its single join window and
        # pass 2 started immediately.)
        for _ in range(30):
            time.sleep(0.1)
            assert _persona_calls["n"] == 1, (
                "pass 2 started while the pass-0 renderer was still alive"
            )
            assert runner.is_alive()
    finally:
        release_render.set()
    # NB: not runner.join(timeout=90) — the join clamp above would
    # shrink that wait to 0.5 s as well. Poll instead.
    deadline = time.time() + 90
    while runner.is_alive() and time.time() < deadline:
        time.sleep(0.2)
    assert not runner.is_alive(), "job thread hung"
    assert "error" not in outcome, f"job died on the stale renderer error: {outcome.get('error')!r}"
    result = outcome["result"]
    assert Path(result["final"]).is_file()
    plan = json.loads(
        (job / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json").read_text(encoding="utf-8")
    )
    assert plan["uniform_restart"] is True
    assert plan["tts_provider"] == "edge_tts"
    # Persona spoke line 2 once in pass 0, then both lines in pass 2.
    assert _persona_calls["n"] == 3
