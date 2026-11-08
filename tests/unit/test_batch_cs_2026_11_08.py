"""Batch CS — recap Gemini route NameError + uniform-voice restart.

Production evidence (job rc_1794110445_124a6b, PURCHASED user):
- The recap CG block in api.py called ``get_or_create_user`` without
  importing it, so EVERY gemini-engine recap since Batch CG raised
  NameError, logged "CG route resolution failed; legacy pool path"
  and ran without BYOK routing / taste accounting.
- The same job's Gemini pool exhausted at line 30/108 and 79 lines
  shipped in the fallback persona voice — a mixed-voice narration.
  CS wires the renderer's (dead) ``restart_uniform`` flag: a Gemini
  pass that falls back on SOME lines restarts the whole TTS stage
  uniformly on the Studio Persona engine (the Batch CE rule).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not available"
)


# ── CS-1: the recap CG block must import its user fetcher ────────────

def test_cs_recap_cg_block_imports_get_or_create_user():
    src = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
    start = src.index("Batch CG: resolve the BYOK route context")
    end = src.index("CG route resolution failed", start)
    block = src[start:end]
    assert "from app.services.ledger import" in block
    assert "get_or_create_user" in block


@pytest.mark.asyncio
async def test_cs_recap_route_resolution_actually_runs():
    """The exact call sequence the recap block performs — previously
    unreachable in production — must build a route context."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.schema import Base
    from app.services.gemini_key_service import resolve_gemini_route
    from app.services.ledger import get_or_create_user

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        owner, _ = await get_or_create_user(session, 919191)
        await session.commit()
        ctx = await resolve_gemini_route(session, owner, is_admin=False)
    await engine.dispose()
    assert ctx is not None
    assert ctx.user_id == 919191
    assert ctx.tier in ("FREE", "PURCHASED")


# ── CS-2: uniform restart in the renderer ────────────────────────────

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
    fail_on = ""  # substring of the narration text that must fail

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


def _patch_all(monkeypatch):
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
    _FakeGemini.fail_on = ""
    _persona_calls["n"] = 0


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


def _out_plan(job: Path):
    return json.loads(
        (job / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json").read_text(encoding="utf-8")
    )


@pytestmark_ffmpeg
def test_cs_mixed_gemini_failure_restarts_uniformly_on_persona(tmp_path, monkeypatch):
    _patch_all(monkeypatch)
    _FakeGemini.fail_on = "ဒုတိယ"  # second line exhausts mid-job
    job = _setup_job(tmp_path)
    result = _run(job)
    assert Path(result["final"]).is_file()
    plan = _out_plan(job)
    # One voice only: the whole narration was redone on the persona
    # engine; no mixed provider, no leftover fallback bookkeeping.
    assert plan["tts_provider"] == "edge_tts"
    assert plan["uniform_restart"] is True
    assert plan["fallback_used"] is False
    assert plan["fallback_segments"] == []
    # Persona spoke line 2 once in pass 0 (the fallback), then BOTH
    # lines again in the uniform pass 2.
    assert _persona_calls["n"] == 3
    ckpt = json.loads((job / "edge_tts_smart_sync" / "checkpoint.json").read_text(encoding="utf-8"))
    assert ckpt["uniform_restart"] is True
    assert ckpt["tts_provider"] == "edge_tts"


@pytestmark_ffmpeg
def test_cs_all_gemini_failure_stays_single_pass(tmp_path, monkeypatch):
    _patch_all(monkeypatch)
    _FakeGemini.fail_on = "စာကြောင်း"  # every line fails → already uniform
    job = _setup_job(tmp_path)
    result = _run(job)
    assert Path(result["final"]).is_file()
    plan = _out_plan(job)
    assert plan["tts_provider"] == "gemini+persona_fallback"
    assert plan["uniform_restart"] is False
    assert plan["fallback_segments"] == [1, 2]
    # No wasteful second pass: persona spoke each line exactly once.
    assert _persona_calls["n"] == 2


@pytestmark_ffmpeg
def test_cs_clean_gemini_pass_unchanged(tmp_path, monkeypatch):
    _patch_all(monkeypatch)
    job = _setup_job(tmp_path)
    result = _run(job)
    assert Path(result["final"]).is_file()
    plan = _out_plan(job)
    assert plan["tts_provider"] == "gemini"
    assert plan["uniform_restart"] is False
    assert _persona_calls["n"] == 0
    assert _FakeGemini.calls == 2
