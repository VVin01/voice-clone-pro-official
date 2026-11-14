"""Batch BV — Recap own-voice narration (Ulfa, 2026-11-06 23:53).

Recap narration accepted only the 12 curated persona slugs; a user's
own uploaded sample could drive the Voice tab's clone jobs but never
a recap. BV closes that gap on the Persona Voice (VoxCPM) engine:

- ``POST /api/recap/upload_voice_ref`` — the recap twin of the voice
  tab's streamed save (50 MB cap, storage/temp/voxcpm_refs/, same
  extension whitelist); the filename embeds the uploader's id
  (``webapp_ref_u<uid>_<uuid12>``) so ownership is a prefix test with
  no registry. PURCHASED + admin only (Option 1 gating, Ulfa
  2026-11-07): FREE gets a clean 403 lock response, never a 500.
- ``RecapConfigRequest`` gains ``voice_ref_id`` (client handle) and
  ``voice_ref_path`` (server-only; the submit endpoint neutralises
  any client-sent value before resolution — a model-level strip
  would break every internal rebuild that round-trips the persisted
  config). Resolution validates exists + owned + audio extension
  BEFORE any credit moves; an invalid ref is a clean 400.
- The resolved path rides the persisted ``recap_config`` JSON and
  the new nullable ``Job.recap_ref_audio_path`` column (BU-pattern
  boot migration). ``source_audio_path`` is NOT reused — on recap
  rows it holds the source VIDEO.
- The executor passes the path into the pipeline's existing
  ``voxcpm_ref_audio`` parameter; the core pre-adapts the reference
  ONCE before the segment loop (BU's 50 s trim), so neither the
  per-segment cost nor the job's total duration grows, and the plan
  JSON records ``reference_trimmed`` for the completion disclosure.
- Tier gating is clamp-never-reject (mirrors AW): a FREE submit or
  execution carrying a ref has it dropped — persona narration, the
  job proceeds. Per-segment clone failures still fall back to the
  picked persona; the bot completion names the own voice honestly
  via ``recap_voice_display_name(..., own_voice=True)``.
- The storage orphan sweep's referenced-paths scan includes the new
  column, so a parked (QUEUED / SCRIPT_READY) job's sample is never
  swept mid-flight.
- Frontend: an own-voice card inside the persona area (shown only
  on the clone engine, locked for FREE like the engine pill),
  upload-on-pick with preview/remove, submit blocked while an
  own-voice upload is in flight, state kept out of the persisted
  recap settings / account prefs. All strings via i18n (my + en).
"""
from __future__ import annotations

import asyncio
import inspect
import io
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.webapp import api as api_mod


REPO_ROOT = Path(api_mod.__file__).resolve().parents[2]
STATIC_DIR = Path(api_mod.__file__).resolve().parent / "static"
APP_JS = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC_DIR / "js" / "i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (STATIC_DIR / "css" / "style.css").read_text(encoding="utf-8")
API_PY = (REPO_ROOT / "app" / "webapp" / "api.py").read_text(encoding="utf-8")
CORE_PY = (REPO_ROOT / "app" / "services" / "recap_engine" / "core.py").read_text(encoding="utf-8")
SCHEMA_PY = (REPO_ROOT / "app" / "models" / "schema.py").read_text(encoding="utf-8")
SESSION_PY = (REPO_ROOT / "app" / "db" / "session.py").read_text(encoding="utf-8")
VOICE_DISPLAY_PY = (REPO_ROOT / "app" / "services" / "voice_display.py").read_text(encoding="utf-8")
LOCALES = {
    lang: json.loads((REPO_ROOT / "locales" / lang / "messages.json").read_text(encoding="utf-8"))
    for lang in ("my", "en")
}

ADMIN_ID = 424242001

pytestmark_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not available"
)


def _write_wav(path: Path, seconds: float = 0.5, rate: int = 8000) -> Path:
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def _ref_file(storage_root: Path, uid: int, suffix: str = "abcdef012345",
              seconds: float = 2.0) -> tuple[str, Path]:
    """Create a real own-voice upload under a storage root; returns
    (ref_id, path) exactly as the upload endpoint would name it."""
    ref_id = f"webapp_ref_u{uid}_{suffix}"
    path = storage_root / "temp" / "voxcpm_refs" / f"{ref_id}.wav"
    _write_wav(path, seconds=seconds)
    return ref_id, path


# ── BV-1: persistence + wiring pins ───────────────────────────────

def test_bv_job_column_and_boot_migrations():
    assert "recap_ref_audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)" in SCHEMA_PY
    assert "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS recap_ref_audio_path TEXT" in SESSION_PY
    assert "ALTER TABLE jobs ADD COLUMN recap_ref_audio_path TEXT" in SESSION_PY
    # The source VIDEO column keeps its own, separate definition.
    assert "source_audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)" in SCHEMA_PY


def test_bv_upload_endpoint_registered_with_auth_gate():
    routes = {
        (r.path, tuple(sorted(getattr(r, "methods", None) or ())))
        for r in api_mod.api_router.routes
    }
    assert ("/api/recap/upload_voice_ref", ("POST",)) in routes
    src = inspect.getsource(api_mod.upload_recap_voice_ref)
    assert "RECAP_OWN_VOICE_VIP_ONLY_DETAIL" in src
    assert "_save_upload_streaming" in src
    assert 'temp" / "voxcpm_refs"' in src or '"voxcpm_refs"' in src
    assert "50 * 1024 * 1024" in src


def test_bv_executor_and_core_wiring():
    # Executor: the validated ref replaces the hardcoded None.
    assert "voxcpm_ref_audio = req.voice_ref_path" in API_PY
    assert "fallback narration_wav = downloader.generate_recap_video(" not in API_PY
    # Core: ONE pre-adapt before the segment loop, feeding the plan.
    assert API_PY.count("_clamp_recap_voice_ref(") >= 3  # def + submit + execution
    assert CORE_PY.count("_adapt_reference_for_clone") == 1
    assert 'checkpoint["reference_trimmed"] = bool(reference_trimmed)' in CORE_PY
    assert 'out_plan["reference_trimmed"] = bool(reference_trimmed)' in CORE_PY
    # The pre-pass sits after persona-ref resolution and before the
    # TTS passes (the segment loop).
    adapt_at = CORE_PY.index("_adapt_reference_for_clone")
    loop_at = CORE_PY.index("for _tts_pass in range(2):")
    assert adapt_at < loop_at


def test_bv_sweep_scan_includes_recap_ref_column():
    src = inspect.getsource(api_mod._sweep_storage_orphans)
    assert "Job.recap_ref_audio_path" in src


def test_bv_display_helper_wiring():
    assert "def own_voice_display_name() -> str:" in VOICE_DISPLAY_PY
    assert "own_voice: bool = False" in VOICE_DISPLAY_PY
    # The completion notify + on-demand caption pass the flag.
    assert API_PY.count("own_voice=bool(") >= 2


# ── BV-2: honesty strings, both languages, every surface ──────────

def test_bv_bot_locales_both_languages():
    for lang in ("my", "en"):
        assert LOCALES[lang].get("recap_own_voice_display"), lang
        note = LOCALES[lang].get("recap_ref_trim_note")
        assert note and note.startswith("\n\n✂️"), lang
        assert "45" in note and "50" in note, lang


def test_bv_miniapp_i18n_keys_both_languages():
    for key in (
        "recap_own_voice_name", "recap_own_voice_desc",
        "recap_own_voice_upload_btn", "recap_own_voice_hint",
        "recap_own_voice_uploading", "recap_own_voice_ready",
        "recap_own_voice_upload_error", "recap_own_voice_pending_error",
        "recap_own_voice_locked_hint", "recap_own_voice_display",
    ):
        assert I18N_JS.count(f"{key}:") == 2, key  # my + en


def test_bv_frontend_card_markup_and_flow():
    # The card lives inside the persona area, after the top-4 grid and
    # before the all-voices expander.
    area = INDEX_HTML.index('id="recap-persona-area"')
    card = INDEX_HTML.index('id="recap-own-voice-card"')
    expander = INDEX_HTML.index('id="btn-toggle-all-voices"')
    assert area < card < expander
    for el in ("recap-own-voice-wrap", "recap-own-voice-input",
               "recap-own-voice-lock-badge", "recap-own-voice-locked-hint",
               "btn-clear-recap-own-voice", "btn-recap-own-voice-play"):
        assert f'id="{el}"' in INDEX_HTML, el
    # Engine visibility + lock application + payload + submit guard.
    areas = APP_JS[APP_JS.index("function syncRecapEngineAreas()"):APP_JS.index("function refreshRecapEngineLock()")]
    assert 'getElementById("recap-own-voice-wrap")' in areas
    lock = APP_JS[APP_JS.index("function refreshRecapEngineLock()"):APP_JS.index("// ── Batch BV: own-voice narration card")]
    assert 'getElementById("recap-own-voice-card")' in lock
    assert 'fetchWithTimeout("/api/recap/upload_voice_ref"' in APP_JS
    assert "voice_ref_id: (state.recapTtsEngine === \"voxcpm\" && state.recapOwnVoiceSelected" in APP_JS
    assert 'state.recapOwnVoiceStatus !== "ready"' in APP_JS
    assert 't("recap_own_voice_pending_error")' in APP_JS
    # Picking a persona deselects own-voice.
    assert "deselectRecapOwnVoice();" in APP_JS
    # Own-voice state never enters the persisted prefs/settings.
    prefs = APP_JS[APP_JS.index("function collectAccountPrefs()"):APP_JS.index("function applyAccountPrefs")]
    assert "recapOwnVoice" not in prefs
    assert "recapOwnVoiceSelected" in APP_JS  # state exists…
    assert "#recap-own-voice-card.engine-locked" in STYLE_CSS


def test_bv_cache_token_is_r93():
    assert INDEX_HTML.count("20261114_r149") == 4
    assert "20261104_r92" not in INDEX_HTML


# ── BV-3: request model validation ────────────────────────────────

def _req(**over):
    base = {"url": "", "title": "T", "voice_slug": "komin", "duration_seconds": 60}
    base.update(over)
    return api_mod.RecapConfigRequest(**base)


def test_bv_ref_id_shape_validation():
    assert _req().voice_ref_id is None
    assert _req(voice_ref_id="").voice_ref_id is None
    ok = _req(voice_ref_id="webapp_ref_u123_ab12cd34ef56")
    assert ok.voice_ref_id == "webapp_ref_u123_ab12cd34ef56"
    for bad in ("webapp_ref_u123_XYZ", "../etc/passwd",
                "webapp_ref_u12_ab12cd34ef5",  # suffix too short
                "webapp_ref_u_ab12cd34ef56"):  # no uid
        with pytest.raises(Exception):
            _req(voice_ref_id=bad)


def test_bv_client_sent_voice_ref_path_is_neutralised_at_submit():
    # The model keeps the field (internal rebuilds round-trip it);
    # the submit endpoint is the neutralisation point.
    src = inspect.getsource(api_mod.create_recap_job)
    assert src.index("req.voice_ref_path = None") < src.index("_clamp_recap_voice_ref")


# ── BV-4: the tier clamp (AW-style unit tests) ────────────────────

def _clamp_req(**over):
    base = dict(voice_ref_id="webapp_ref_u1_abcdef012345",
                voice_ref_path="/srv/ref.wav", tts_engine="voxcpm")
    base.update(over)
    return SimpleNamespace(**base)


def test_bv_clamp_free_drops_ref_at_submit_and_execution(caplog):
    for is_vip in (False,):
        req = _clamp_req()
        with caplog.at_level(logging.INFO, logger="app.webapp.api"):
            out = api_mod._clamp_recap_voice_ref(
                req,
                user=SimpleNamespace(id=7, account_category="FREE"),
                user_id=7, is_vip=is_vip,
            )
        assert out is None and req.voice_ref_id is None and req.voice_ref_path is None
    assert any("clamped" in r.message for r in caplog.records)


def test_bv_clamp_keeps_ref_for_purchased_and_admin():
    user = SimpleNamespace(id=7, is_admin=False, account_category="PURCHASED")
    req = _clamp_req()
    out = api_mod._clamp_recap_voice_ref(req, user=user, user_id=7, is_vip=True)
    assert out == "/srv/ref.wav"
    assert req.voice_ref_id == "webapp_ref_u1_abcdef012345"

    admin = SimpleNamespace(id=ADMIN_ID, is_admin=True, account_category="FREE")
    req2 = _clamp_req()
    out2 = api_mod._clamp_recap_voice_ref(req2, user=admin, user_id=ADMIN_ID, is_vip=True)
    assert out2 == "/srv/ref.wav"


def test_bv_clamp_user_object_wins_over_flag():
    # A FREE user object clamps even if a caller passed is_vip=True —
    # the user row is the tier's source of truth.
    free = SimpleNamespace(id=7, is_admin=False, account_category="FREE")
    req = _clamp_req()
    out = api_mod._clamp_recap_voice_ref(req, user=free, user_id=7, is_vip=True)
    assert out is None and req.voice_ref_path is None


# ── BV-5: the upload endpoint (AJ-style direct calls) ─────────────

class _FakeUpload:
    """Minimal UploadFile stand-in: async read(n) over a BytesIO."""

    def __init__(self, filename, data: bytes):
        self.filename = filename
        self._buf = io.BytesIO(data)
        self.bytes_read = 0

    async def read(self, n: int = -1) -> bytes:
        chunk = self._buf.read(n)
        self.bytes_read += len(chunk)
        return chunk


def _audio_bytes(seconds: float = 1.0, rate: int = 8000) -> bytes:
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def _call_upload(tmp_path, monkeypatch, *, user, filename="sample.wav",
                 data=None):
    from app.config import settings as settings_obj

    monkeypatch.setattr(settings_obj, "storage_root", tmp_path)
    monkeypatch.setattr(settings_obj, "admin_ids_raw", str(ADMIN_ID))
    monkeypatch.setattr(api_mod, "DISK_HEADROOM_MIN_FREE_BYTES", 0)
    upload = _FakeUpload(filename, _audio_bytes() if data is None else data)
    return asyncio.run(
        api_mod.upload_recap_voice_ref(
            reference_audio=upload, user=user, _gate=user
        )
    ), upload


def test_bv_upload_saves_with_ownership_prefix(tmp_path, monkeypatch):
    user = SimpleNamespace(id=880201, is_admin=False, account_category="PURCHASED")
    result, _ = _call_upload(tmp_path, monkeypatch, user=user)
    assert result["success"] is True
    ref_id = result["ref_id"]
    assert ref_id.startswith("webapp_ref_u880201_")
    saved = tmp_path / "temp" / "voxcpm_refs" / f"{ref_id}.wav"
    assert saved.is_file() and saved.stat().st_size == result["size_bytes"]


def test_bv_upload_free_is_locked_not_500(tmp_path, monkeypatch):
    user = SimpleNamespace(id=880202, is_admin=False, account_category="FREE")
    with pytest.raises(HTTPException) as excinfo:
        _call_upload(tmp_path, monkeypatch, user=user)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == api_mod.RECAP_OWN_VOICE_VIP_ONLY_DETAIL
    assert not list((tmp_path / "temp" / "voxcpm_refs").glob("*")) \
        if (tmp_path / "temp" / "voxcpm_refs").exists() else True


def test_bv_upload_admin_allowed_and_ext_coerced(tmp_path, monkeypatch):
    user = SimpleNamespace(id=ADMIN_ID, is_admin=True, account_category="FREE")
    result, _ = _call_upload(tmp_path, monkeypatch, user=user, filename="clip.aac")
    saved = tmp_path / "temp" / "voxcpm_refs" / f"{result['ref_id']}.wav"
    assert saved.is_file()  # .aac coerced to .wav like the voice route


def test_bv_upload_oversize_413_and_consumer_stops(tmp_path, monkeypatch):
    user = SimpleNamespace(id=880203, is_admin=False, account_category="PURCHASED")
    big = b"\x00" * (51 * 1024 * 1024)
    with pytest.raises(HTTPException) as excinfo:
        _call_upload(tmp_path, monkeypatch, user=user, data=big)
    assert excinfo.value.status_code == 413


def test_bv_upload_tiny_file_discarded(tmp_path, monkeypatch):
    user = SimpleNamespace(id=880204, is_admin=False, account_category="PURCHASED")
    with pytest.raises(HTTPException) as excinfo:
        _call_upload(tmp_path, monkeypatch, user=user, data=b"tiny")
    assert excinfo.value.status_code == 400
    ref_dir = tmp_path / "temp" / "voxcpm_refs"
    assert not ref_dir.exists() or not list(ref_dir.glob("*"))


# ── BV-6: submit path against a real ledger DB (AO harness) ──────

class _LedgerHandle:
    def __init__(self, db_file: str):
        self._db_file = db_file

    def make_session(self):
        from sqlalchemy.ext.asyncio import (
            AsyncSession,
            async_sessionmaker,
            create_async_engine,
        )

        engine = create_async_engine(
            f"sqlite+aiosqlite:///{self._db_file}", future=True
        )
        return async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )()

    def credits(self, uid) -> int:
        from app.models.schema import User

        async def _read():
            async with self.make_session() as s:
                user = await s.get(User, uid)
                return user.recap_credits if user else None

        return asyncio.run(_read())

    def job_row(self, job_id):
        from app.models.schema import Job

        async def _read():
            async with self.make_session() as s:
                job = await s.get(Job, job_id)
                if job is None:
                    return None
                return {
                    "recap_ref_audio_path": job.recap_ref_audio_path,
                    "recap_config": job.recap_config,
                }

        return asyncio.run(_read())


@pytest.fixture()
def ledger(monkeypatch, tmp_path):
    from sqlalchemy import create_engine

    import app.models.schema  # noqa: F401 — register all models
    from app.config import settings as settings_obj
    from app.db.base import Base

    db_file = str(tmp_path / "batch_bv.db")
    sync_engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    handle = _LedgerHandle(db_file)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", handle.make_session)
    monkeypatch.setattr(settings_obj, "admin_ids_raw", str(ADMIN_ID))
    monkeypatch.setattr(settings_obj, "tiered_recap_pricing_enabled", False)
    monkeypatch.setattr(settings_obj, "storage_root", tmp_path)
    monkeypatch.setattr(api_mod, "DISK_HEADROOM_MIN_FREE_BYTES", 0)
    yield handle
    api_mod.RECAP_JOBS_REGISTRY.clear()


def _submit(ledger, monkeypatch, *, uid, credits=5, category="PURCHASED",
            **req_over):
    """Drive create_recap_job with a fake runner; returns
    (outcome, captured) where captured lists (job_id, req)."""
    from app.models.schema import MinuteBalance, User

    captured = []

    async def _fake_runner(job_id, req, user_id, chat_id, **kwargs):
        captured.append((job_id, req))

    monkeypatch.setattr(api_mod, "_run_recap_background_job", _fake_runner)

    base = {"url": "", "title": "Test Movie", "voice_slug": "komin",
            "duration_seconds": 60}
    base.update(req_over)

    async def _main():
        async with ledger.make_session() as s:
            user = await s.get(User, uid)
            if user is None:
                s.add(User(id=uid, recap_credits=credits,
                           account_category=category))
                # Phase 5 Option B: VoxCPM clone-voice jobs pre-check & hold Voice Clone Minutes.
                s.add(MinuteBalance(user_id=uid, purchased_seconds=1800))
                await s.commit()
                user = await s.get(User, uid)
            req = api_mod.RecapConfigRequest(**base)
            try:
                resp = await api_mod.create_recap_job(req, user=user, db=s, _gate=user)
            except HTTPException as exc:
                return ("error", exc.status_code, exc.detail)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return ("ok", resp)

    return asyncio.run(_main()), captured


def test_bv_submit_purchased_ref_persists_everywhere(ledger, monkeypatch, tmp_path):
    uid = 880301
    ref_id, ref_path = _ref_file(tmp_path, uid)
    (outcome, captured) = _submit(
        ledger, monkeypatch, uid=uid, voice_ref_id=ref_id
    )
    assert outcome[0] == "ok", outcome
    job_id = outcome[1]["recap_id"]
    # Dispatched request carries the resolved server-side path.
    assert captured and captured[0][1].voice_ref_path == str(ref_path)
    # Job row column + persisted config both carry it.
    row = ledger.job_row(job_id)
    assert row["recap_ref_audio_path"] == str(ref_path)
    cfg = json.loads(row["recap_config"])["config"]
    assert cfg["voice_ref_path"] == str(ref_path)
    # Registry honesty flags.
    entry = api_mod.RECAP_JOBS_REGISTRY[job_id]
    assert entry["voice_ref_used"] is True
    assert ledger.credits(uid) == 4  # exactly one submit credit moved


def test_bv_submit_unknown_ref_rejected_before_credits(ledger, monkeypatch, tmp_path):
    uid = 880302
    (outcome, captured) = _submit(
        ledger, monkeypatch, uid=uid,
        voice_ref_id=f"webapp_ref_u{uid}_000000000000",
    )
    assert outcome[0] == "error" and outcome[1] == 400
    assert captured == []  # nothing dispatched
    assert ledger.credits(uid) == 5  # balance untouched


def test_bv_submit_other_users_ref_rejected(ledger, monkeypatch, tmp_path):
    uid = 880303
    ref_id, _ = _ref_file(tmp_path, 880399)  # owned by someone else
    (outcome, captured) = _submit(ledger, monkeypatch, uid=uid, voice_ref_id=ref_id)
    assert outcome[0] == "error" and outcome[1] == 400
    assert captured == []
    assert ledger.credits(uid) == 5


def test_bv_submit_free_ref_is_clamped_job_proceeds(ledger, monkeypatch, tmp_path):
    uid = 880304
    ref_id, _ = _ref_file(tmp_path, uid)
    (outcome, captured) = _submit(
        ledger, monkeypatch, uid=uid, category="FREE", voice_ref_id=ref_id
    )
    assert outcome[0] == "ok", outcome
    job_id = outcome[1]["recap_id"]
    assert captured[0][1].voice_ref_path is None
    assert captured[0][1].voice_ref_id is None
    row = ledger.job_row(job_id)
    assert row["recap_ref_audio_path"] is None
    assert api_mod.RECAP_JOBS_REGISTRY[job_id]["voice_ref_used"] is False
    assert ledger.credits(uid) == 4  # normal persona charge, no error


def test_bv_submit_admin_ref_kept_despite_free_category(ledger, monkeypatch, tmp_path):
    # Admin is id-based (settings.is_admin); a FREE-category admin
    # keeps the ref — the tier helper's admin branch decides.
    ref_id, ref_path = _ref_file(tmp_path, ADMIN_ID)
    (outcome, captured) = _submit(
        ledger, monkeypatch, uid=ADMIN_ID, category="FREE",
        voice_ref_id=ref_id,
    )
    assert outcome[0] == "ok", outcome
    assert captured[0][1].voice_ref_path == str(ref_path)


def test_bv_submit_forged_voice_ref_path_ignored(ledger, monkeypatch, tmp_path):
    uid = 880305
    (outcome, captured) = _submit(
        ledger, monkeypatch, uid=uid, voice_ref_path="/etc/passwd"
    )
    assert outcome[0] == "ok", outcome
    assert captured[0][1].voice_ref_path is None
    row = ledger.job_row(outcome[1]["recap_id"])
    assert row["recap_ref_audio_path"] is None


def test_bv_submit_ref_dropped_on_gemini_engine(ledger, monkeypatch, tmp_path):
    uid = 880306
    ref_id, _ = _ref_file(tmp_path, uid)
    (outcome, captured) = _submit(
        ledger, monkeypatch, uid=uid, tts_engine="gemini",
        gemini_voice="Puck", voice_ref_id=ref_id,
    )
    assert outcome[0] == "ok", outcome
    assert captured[0][1].voice_ref_path is None
    assert captured[0][1].voice_ref_id is None


def test_bv_persisted_config_round_trip_preserves_ref(ledger, monkeypatch, tmp_path):
    from app.models.schema import Job

    uid = 880307
    ref_id, ref_path = _ref_file(tmp_path, uid)
    (outcome, _) = _submit(ledger, monkeypatch, uid=uid, voice_ref_id=ref_id)
    assert outcome[0] == "ok"
    job_id = outcome[1]["recap_id"]

    async def _rebuild():
        async with ledger.make_session() as s:
            job = await s.get(Job, job_id)
            return api_mod._recap_job_config(job)

    rebuilt = asyncio.run(_rebuild())
    assert rebuilt is not None
    assert rebuilt.voice_ref_path == str(ref_path)
    assert rebuilt.voice_ref_id == ref_id


# ── BV-7: core — one-time adapt, plan flag, persona fallback ─────

def _sine_wav(path: Path, seconds: float = 0.6, freq: int = 660) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}",
         "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(path)],
        check=True,
    )
    return path


class _SpyVox:
    """VoxCPMEngine stand-in: synthesize is faked, but the reference
    adaptation delegates to a REAL engine so the BU trim behaviour
    (probe, one trim, shared-work-dir reuse) is genuinely exercised."""

    adapt_calls: list = []
    synth_refs: list = []
    fail_texts: set = set()
    _delegate = None

    def __init__(self, **kw):
        self.work_dir = Path(kw.get("work_dir") or ".")
        self._log = kw.get("log_fn") or (lambda m: None)

    def _real_engine(self):
        if _SpyVox._delegate is None:
            from app.services.voxcpm_engine import VoxCPMEngine as RealEngine

            _SpyVox._delegate = RealEngine(work_dir=self.work_dir)
        return _SpyVox._delegate

    @property
    def last_reference_trimmed(self):
        delegate = _SpyVox._delegate
        return bool(delegate is not None and delegate.last_reference_trimmed)

    def _adapt_reference_for_clone(self, ref):
        _SpyVox.adapt_calls.append(str(ref))
        return self._real_engine()._adapt_reference_for_clone(Path(ref))

    def synthesize(self, text, output_wav, **kw):
        _SpyVox.synth_refs.append((text, kw.get("reference_wav")))
        if text in _SpyVox.fail_texts:
            raise RuntimeError("fake vox failure")
        return _sine_wav(Path(output_wav), 0.6, freq=440)


def _setup_core_job(tmp_path: Path, ref: Path) -> Path:
    job = tmp_path / "job"
    job.mkdir(parents=True, exist_ok=True)
    src = job / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=duration=3:size=320x180:rate=30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(src)],
        check=True,
    )
    plan = {"segments": [
        {"video_start": 0.0, "video_end": 1.5, "narration": "ပထမ စာကြောင်း ဖြစ်ပါတယ်"},
        {"video_start": 1.5, "video_end": 3.0, "narration": "ဒုတိယ စာကြောင်း ဖြစ်ပါတယ်"},
    ]}
    (job / "smart_sync_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8"
    )
    return job


def _run_core_job(job: Path, ref: Path):
    from app.services.recap_engine.core import EdgeSmartSync

    sync = EdgeSmartSync(
        job_dir=str(job),
        source_path=str(job / "source.mp4"),
        plan_path=str(job / "smart_sync_plan.json"),
        voice="voxcpm_komin",
        output_ratio="Original",
        mirror_video=False,
        zoom_factor=1.0,
        overlays={"tts_engine": "voxcpm", "voxcpm_ref_audio": str(ref),
                  "is_female": False},
        log=lambda m: None,
        tts_workers=1,
    )
    return sync.run()


def _patch_core(monkeypatch):
    import app.services.recap_engine.voxcpm_engine as vox_mod
    from app.services import voxcpm_engine as vx

    monkeypatch.setattr(vox_mod, "VoxCPMEngine", _SpyVox)
    monkeypatch.setattr(vx, "get_available_hf_tokens", lambda: [])
    _SpyVox.adapt_calls = []
    _SpyVox.synth_refs = []
    _SpyVox.fail_texts = set()
    _SpyVox._delegate = None


@pytestmark_ffmpeg
def test_bv_core_long_ref_adapted_once_and_flagged(tmp_path, monkeypatch):
    _patch_core(monkeypatch)
    ref = _write_wav(tmp_path / "own_ref.wav", seconds=55, rate=8000)
    job = _setup_core_job(tmp_path, ref)
    result = _run_core_job(job, ref)

    assert Path(result["final"]).is_file()
    # Exactly ONE core pre-adapt for the whole multi-segment job…
    assert len(_SpyVox.adapt_calls) == 1
    # …producing exactly ONE trimmed copy in the shared work dir…
    trimmed = list((job / "edge_tts_smart_sync" / "work" / "voxcpm_temp").glob("ref_trim_*.wav"))
    assert len(trimmed) == 1
    # …and every segment cloned from the trimmed copy, never the raw 55 s file.
    assert _SpyVox.synth_refs, "no clone calls happened"
    assert all(r == str(trimmed[0]) for _t, r in _SpyVox.synth_refs)
    # The plan JSON carries the trim flag for the disclosure surfaces.
    out_plan = json.loads(
        (job / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json").read_text(encoding="utf-8")
    )
    assert out_plan["reference_trimmed"] is True
    assert out_plan["fallback_segments"] == []


@pytestmark_ffmpeg
def test_bv_core_short_ref_no_trim(tmp_path, monkeypatch):
    _patch_core(monkeypatch)
    ref = _write_wav(tmp_path / "own_ref.wav", seconds=10, rate=8000)
    job = _setup_core_job(tmp_path, ref)
    result = _run_core_job(job, ref)

    assert Path(result["final"]).is_file()
    assert len(_SpyVox.adapt_calls) == 1
    assert all(r == str(ref) for _t, r in _SpyVox.synth_refs)
    out_plan = json.loads(
        (job / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json").read_text(encoding="utf-8")
    )
    assert out_plan["reference_trimmed"] is False


@pytestmark_ffmpeg
def test_bv_core_failed_segment_falls_back_to_persona(tmp_path, monkeypatch):
    import app.services.curated_voices as cv

    _patch_core(monkeypatch)
    persona_calls = []

    def _fake_persona(text, persona_id, output_wav, speed=1.1, log_fn=None):
        persona_calls.append((text, persona_id))
        return _sine_wav(Path(output_wav), 0.6, freq=330)

    monkeypatch.setattr(cv, "synthesize_curated_persona", _fake_persona)
    _SpyVox.fail_texts = {"ဒုတိယ စာကြောင်း ဖြစ်ပါတယ်"}
    ref = _write_wav(tmp_path / "own_ref.wav", seconds=10, rate=8000)
    job = _setup_core_job(tmp_path, ref)
    result = _run_core_job(job, ref)

    assert Path(result["final"]).is_file()
    out_plan = json.loads(
        (job / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json").read_text(encoding="utf-8")
    )
    assert out_plan["fallback_used"] is True
    assert out_plan["fallback_segments"] == [2]
    # The fallback narrated segment 2 with the picked persona.
    assert persona_calls and persona_calls[0][1] == "komin"


# ── BV-8: the orphan sweep keeps a parked job's sample ────────────

def test_bv_sweep_keeps_referenced_ref_sweeps_orphan(ledger, monkeypatch, tmp_path):
    from app.models.schema import Job, User

    uid = 880401
    ref_id, ref_path = _ref_file(tmp_path, uid)
    orphan = tmp_path / "temp" / "voxcpm_refs" / "webapp_ref_u880499_1234567890ab.wav"
    _write_wav(orphan, seconds=1.0)
    old = time.time() - (48 * 3600)
    os.utime(ref_path, (old, old))
    os.utime(orphan, (old, old))

    async def _seed():
        async with ledger.make_session() as s:
            s.add(User(id=uid, recap_credits=5, account_category="PURCHASED"))
            s.add(Job(
                id="bv-sweep-job", user_id=uid, chat_id=uid,
                status="QUEUED", job_type="RECAP",
                recap_ref_audio_path=str(ref_path),
            ))
            await s.commit()

    asyncio.run(_seed())
    asyncio.run(
        api_mod._sweep_storage_orphans(
            retention_cutoff_ts=time.time() - 24 * 3600
        )
    )
    assert ref_path.is_file(), "a parked job's sample must survive the sweep"
    assert not orphan.exists(), "an unreferenced old sample must be swept"


# ── BV-9: display-name honesty ────────────────────────────────────

def test_bv_own_voice_display_name():
    from app.services.voice_display import (
        own_voice_display_name,
        recap_voice_display_name,
    )

    assert own_voice_display_name() == "🎙️ ကိုယ်ပိုင်အသံ"
    assert recap_voice_display_name("voxcpm", None, "komin", own_voice=True) \
        == "🎙️ ကိုယ်ပိုင်အသံ"
    # Persona-only behaviour is byte-identical to pre-BV…
    assert recap_voice_display_name("voxcpm", None, "komin") != "🎙️ ကိုယ်ပိုင်အသံ"
    # …and the flag never leaks into the Gemini/Edge branches.
    assert recap_voice_display_name("gemini", "Puck", "komin", own_voice=True) \
        == recap_voice_display_name("gemini", "Puck", "komin")
    assert recap_voice_display_name("edge", None, "komin", own_voice=True) \
        == recap_voice_display_name("edge", None, "komin")
