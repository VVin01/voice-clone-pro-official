"""Functional regression tests for the Gemini TTS wiring in
EdgeSmartSync (production-grade Feature 5/7).

Runs a real (tiny) EdgeSmartSync pass with all network engines faked:
- Gemini selected with a persona voice ("voxcpm_komin") must narrate
  via Gemini ONLY — the VoxCPM branch must not overwrite Gemini
  segments (use_voxcpm is also true for persona voices; regression
  guard for the gate added in the robustness pass).
- Gemini failure must degrade per segment to the persona engine with
  fallback_used/fallback_segments recorded — never silently.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")


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


class _FakeVox:
    calls = 0

    def __init__(self, **kw):
        pass

    def synthesize(self, **kw):
        _FakeVox.calls += 1
        raise AssertionError("VoxCPM must not run when Gemini TTS is selected")


class _FakeGemini:
    calls = 0
    fail = False

    def __init__(self, **kw):
        self.work_dir = Path(kw.get("work_dir") or ".")

    def generate_channel99_voice(self, text, voice):
        _FakeGemini.calls += 1
        if _FakeGemini.fail:
            raise RuntimeError("fake gemini quota exhausted")
        return _write_wav(self.work_dir / f"gem_{_FakeGemini.calls}.wav", freq=660)


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


def _patch_all(monkeypatch):
    import app.services.curated_voices as cv
    import app.services.recap_engine.core as core
    import app.services.recap_engine.gemini_tts_engine as gem_mod
    import app.services.recap_engine.voxcpm_engine as vox_mod

    monkeypatch.setattr(core, "EdgeTTSEngine", _FakeEdge)
    monkeypatch.setattr(vox_mod, "VoxCPMEngine", _FakeVox)
    monkeypatch.setattr(gem_mod, "GeminiTTSEngine", _FakeGemini)
    monkeypatch.setattr(gem_mod, "collect_gemini_keys", lambda: ["dummy-key"])

    def _fake_persona(text, persona_id, output_wav, speed=1.1, log_fn=None):
        return _write_wav(Path(output_wav), freq=550)

    monkeypatch.setattr(cv, "synthesize_curated_persona", _fake_persona)
    _FakeVox.calls = 0
    _FakeGemini.calls = 0
    _FakeGemini.fail = False


def _run(job: Path):
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
    return sync.run()


def test_gemini_selected_voxcpm_branch_never_overwrites(tmp_path, monkeypatch):
    _patch_all(monkeypatch)
    job = _setup_job(tmp_path)
    result = _run(job)
    assert Path(result["final"]).is_file()
    assert _FakeGemini.calls == 2
    assert _FakeVox.calls == 0
    out_plan = json.loads((job / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json").read_text(encoding="utf-8"))
    assert out_plan["tts_provider"] == "gemini"
    assert out_plan["fallback_used"] is False
    assert out_plan["fallback_segments"] == []


def test_gemini_failure_falls_back_per_segment_visibly(tmp_path, monkeypatch):
    _patch_all(monkeypatch)
    _FakeGemini.fail = True
    job = _setup_job(tmp_path)
    result = _run(job)
    assert Path(result["final"]).is_file()  # job still completes via persona fallback
    out_plan = json.loads((job / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json").read_text(encoding="utf-8"))
    assert out_plan["tts_provider"] == "gemini+persona_fallback"
    assert out_plan["fallback_used"] is True
    assert out_plan["fallback_segments"] == [1, 2]
