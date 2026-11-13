"""Batch BU — clone reference auto-trim (production job ed0ff7fe).

A user uploaded a clone reference LONGER than the VoxCPM Space cap
(50 s). Every admitting Space rejected it ("Reference audio is too
long. Please upload audio no longer than 50 seconds."), the engine
exhausted its walk, and the job silently fell back to the Edge TTS
persona voice — the user paid 15 s for a voice that was not their
clone. The intake never warned and the engine never adapted the
reference.

The fix makes the engine adapt the reference instead of failing:

- synthesize() probes the reference ONCE (new soft-fail ffprobe
  helper — the codebase's other probes do not fit: audio's raises by
  design and returns whole seconds; downloader's drags yt_dlp);
- over 50 s → the clone runs from a trimmed copy holding the FIRST
  45 s, written into the engine work dir (the job-scoped temp dir,
  so it dies with the job's existing temp lifecycle) and used for
  EVERY Space attempt in the call;
- at/under the cap, an unprobeable reference, or a failed trim →
  the ORIGINAL file, byte-identical to pre-BU behaviour (the trim
  machinery can never fail a working job);
- the outcome is published on engine.last_reference_trimmed
  (mirroring last_fallback_used), persisted by the voice worker on
  the new Job.reference_trimmed column, and disclosed to the user:
  bot delivery note (worker_ref_trim_note) + Mini App result card,
  completion toasts, history detail note and history badge
  (voice_ref_trim_notice / hist_ref_trim_badge) — i18n in my + en
  on every surface, no hardcoded UI text.

Recap inherits the trim mechanically (its synthesize calls pass
persona references through the same engine entry) but reads no trim
flag — its references are the curated persona samples, short by
construction. Quota/admission, the Space walk order, fallback
behaviour, billing and the 24 h retention are untouched.
"""
from __future__ import annotations

import json
import wave
from pathlib import Path

from app.services import voxcpm_engine as vx
from app.services.voxcpm_engine import VoxCPMEngine
from app.webapp import api as api_mod


REPO_ROOT = Path(api_mod.__file__).resolve().parents[2]
STATIC_DIR = Path(api_mod.__file__).resolve().parent / "static"
APP_JS = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC_DIR / "js" / "i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
ENGINE_PY = (REPO_ROOT / "app" / "services" / "voxcpm_engine.py").read_text(encoding="utf-8")
WORKER_PY = (REPO_ROOT / "app" / "worker.py").read_text(encoding="utf-8")
API_PY = (REPO_ROOT / "app" / "webapp" / "api.py").read_text(encoding="utf-8")
SCHEMA_PY = (REPO_ROOT / "app" / "models" / "schema.py").read_text(encoding="utf-8")
SESSION_PY = (REPO_ROOT / "app" / "db" / "session.py").read_text(encoding="utf-8")
LOCALES = {
    lang: json.loads((REPO_ROOT / "locales" / lang / "messages.json").read_text(encoding="utf-8"))
    for lang in ("my", "en")
}


# ── behavioural harness (pattern from test_voxcpm_failover_fix) ──

def _write_wav(path: Path, seconds: float = 0.5, rate: int = 8000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


class _FakeJob:
    def __init__(self, result_path):
        self._result_path = result_path

    def done(self):
        return True

    def status(self):
        return None

    def result(self):
        return str(self._result_path)


class _FakeClient:
    """Records the full submit kwargs so tests can see WHICH
    reference file actually reached the Space."""

    def __init__(self, result_path, record: list):
        self._result_path = result_path
        self.record = record

    def submit(self, **kwargs):
        self.record.append(kwargs)
        return _FakeJob(self._result_path)


def _make_engine(tmp_path, monkeypatch, record: list):
    monkeypatch.setattr(vx, "get_available_hf_tokens", lambda: [])
    engine = VoxCPMEngine(work_dir=tmp_path / "cache")
    engine.spaces = ["yeyee2609/voxcpm2"]
    engine.hf_tokens = []
    ok_wav = _write_wav(tmp_path / "ok.wav", seconds=0.5, rate=24000)
    client = _FakeClient(ok_wav, record)
    def _fake_get_client(cls, space, hf_token=None):
        return client

    monkeypatch.setattr(VoxCPMEngine, "get_client", classmethod(_fake_get_client))
    return engine


def _synth(engine, tmp_path, ref):
    return engine.synthesize(
        text="clone this voice exactly",
        output_wav=tmp_path / "out.wav",
        reference_wav=ref,
        prompt_text="reference transcript",
        timeout=5,
        total_timeout=30,
    )


def _ref_path_sent(record: list) -> str:
    assert record, "the Space never received a clone call"
    ref = record[0].get("ref_audio_path")
    assert ref is not None, "clone call carried no reference"
    return ref["path"] if isinstance(ref, dict) else str(ref)


# ── BU-1: over-cap reference is trimmed, trimmed file is used ────

def test_bu_over_cap_reference_trimmed_and_trimmed_file_reaches_space(tmp_path, monkeypatch):
    record: list = []
    engine = _make_engine(tmp_path, monkeypatch, record)
    ref = _write_wav(tmp_path / "ref_long.wav", seconds=55, rate=8000)

    out = _synth(engine, tmp_path, ref)

    assert Path(out).is_file()
    sent = _ref_path_sent(record)
    assert sent != str(ref.resolve())
    assert "ref_trim_" in Path(sent).name
    assert Path(sent).is_file()
    # The trimmed copy really holds the first 45 s.
    dur = vx._probe_reference_duration_seconds(Path(sent))
    assert dur is not None and 44.0 <= dur <= 46.0
    # Honesty state published for the caller.
    assert engine.last_reference_trimmed is True
    assert engine.last_reference_original_seconds is not None
    assert 54.0 <= engine.last_reference_original_seconds <= 56.0
    assert engine.last_reference_trimmed_seconds == vx.REFERENCE_TRIM_SECONDS == 45.0


def test_bu_adapter_reuses_existing_trimmed_copy(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path, monkeypatch, [])
    ref = _write_wav(tmp_path / "ref.wav", seconds=55, rate=8000)
    calls: list = []

    def _counting_trim(src, dst, seconds):
        calls.append(dst)
        dst.write_bytes(b"trimmed")
        return True

    monkeypatch.setattr(vx, "_trim_reference_audio", _counting_trim)
    first = engine._adapt_reference_for_clone(ref)
    second = engine._adapt_reference_for_clone(ref)
    assert first == second
    assert first.name.startswith("ref_trim_")
    assert len(calls) == 1  # second call reuses the copy, no re-trim


# ── BU-2: at/under cap — byte-identical to today ─────────────────

def test_bu_under_cap_reference_untouched(tmp_path, monkeypatch):
    record: list = []
    engine = _make_engine(tmp_path, monkeypatch, record)
    ref = _write_wav(tmp_path / "ref_short.wav", seconds=10, rate=8000)

    _synth(engine, tmp_path, ref)

    assert _ref_path_sent(record) == str(ref.resolve())
    assert engine.last_reference_trimmed is False
    assert not list((tmp_path / "cache").glob("ref_trim_*"))


def test_bu_exactly_at_cap_reference_untouched(tmp_path, monkeypatch):
    record: list = []
    engine = _make_engine(tmp_path, monkeypatch, record)
    ref = _write_wav(tmp_path / "ref.wav", seconds=1, rate=8000)
    monkeypatch.setattr(vx, "_probe_reference_duration_seconds", lambda p: 50.0)

    _synth(engine, tmp_path, ref)

    assert _ref_path_sent(record) == str(ref.resolve())
    assert engine.last_reference_trimmed is False
    assert not list((tmp_path / "cache").glob("ref_trim_*"))


# ── BU-3: probe/trim failure falls through with the original ─────

def test_bu_probe_failure_uses_original_reference(tmp_path, monkeypatch):
    record: list = []
    engine = _make_engine(tmp_path, monkeypatch, record)
    ref = _write_wav(tmp_path / "ref_long.wav", seconds=55, rate=8000)
    monkeypatch.setattr(vx, "_probe_reference_duration_seconds", lambda p: None)

    _synth(engine, tmp_path, ref)

    assert _ref_path_sent(record) == str(ref.resolve())
    assert engine.last_reference_trimmed is False
    assert not list((tmp_path / "cache").glob("ref_trim_*"))


def test_bu_trim_failure_uses_original_reference(tmp_path, monkeypatch):
    record: list = []
    engine = _make_engine(tmp_path, monkeypatch, record)
    ref = _write_wav(tmp_path / "ref_long.wav", seconds=55, rate=8000)
    monkeypatch.setattr(vx, "_probe_reference_duration_seconds", lambda p: 75.0)
    monkeypatch.setattr(vx, "_trim_reference_audio", lambda s, d, sec: False)

    _synth(engine, tmp_path, ref)

    assert _ref_path_sent(record) == str(ref.resolve())
    assert engine.last_reference_trimmed is False


def test_bu_flag_set_only_when_a_trim_actually_happened(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path, monkeypatch, [])
    # synthesize() owns the per-call state initialisation (the same
    # pattern as last_fallback_used); seed it here for the direct
    # adapter calls below.
    engine.last_reference_trimmed = False
    engine.last_reference_original_seconds = None
    engine.last_reference_trimmed_seconds = None
    ref = _write_wav(tmp_path / "ref.wav", seconds=1, rate=8000)

    # Probe says nothing is known → no trim, no flag, same path.
    monkeypatch.setattr(vx, "_probe_reference_duration_seconds", lambda p: None)
    assert engine._adapt_reference_for_clone(ref) == ref
    assert engine.last_reference_trimmed is False

    # Probe says over cap, trim succeeds → trimmed path + flag.
    monkeypatch.setattr(vx, "_probe_reference_duration_seconds", lambda p: 90.0)
    monkeypatch.setattr(
        vx, "_trim_reference_audio",
        lambda s, d, sec: (d.write_bytes(b"x" * 64), True)[1],
    )
    out = engine._adapt_reference_for_clone(ref)
    assert out != ref and out.name.startswith("ref_trim_")
    assert engine.last_reference_trimmed is True
    assert engine.last_reference_original_seconds == 90.0


# ── BU-4: engine wiring pins ─────────────────────────────────────

def test_bu_engine_constants_and_call_site():
    assert "REFERENCE_MAX_SECONDS = 50.0" in ENGINE_PY
    assert "REFERENCE_TRIM_SECONDS = 45.0" in ENGINE_PY
    # The adaptation happens ONCE at reference load, before handle_file,
    # so every attempt in the call shares the trimmed copy.
    prep = ENGINE_PY.index("ref_path = self._adapt_reference_for_clone(ref_path)")
    handle = ENGINE_PY.index("ref_file = handle_file(str(ref_path.resolve()))")
    assert prep < handle
    # Transparency state is (re)initialised per synthesize() call.
    assert "self.last_reference_trimmed = False" in ENGINE_PY
    assert "self.last_reference_trimmed = True" in ENGINE_PY


def test_bu_engine_probe_is_soft_fail_and_local_only():
    assert "def _probe_reference_duration_seconds(path: Path) -> Optional[float]:" in ENGINE_PY
    assert '"-protocol_whitelist", "file,pipe"' in ENGINE_PY  # AJ L21: local files only
    assert "def _trim_reference_audio(src: Path, dst: Path, seconds: float) -> bool:" in ENGINE_PY


# ── BU-5: persistence — column + boot migrations + worker ────────

def test_bu_job_column_and_boot_migrations():
    assert "reference_trimmed: Mapped[bool] = mapped_column(Boolean" in SCHEMA_PY
    assert 'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS reference_trimmed BOOLEAN DEFAULT FALSE' in SESSION_PY
    assert 'ALTER TABLE jobs ADD COLUMN reference_trimmed BOOLEAN DEFAULT 0' in SESSION_PY


def test_bu_worker_reads_flag_persists_and_notes_delivery():
    assert 'getattr(engine_inst, "last_reference_trimmed", False)' in WORKER_PY
    assert "job.reference_trimmed = True" in WORKER_PY
    assert 'trim_note = t("worker_ref_trim_note", lang) if reference_trimmed else ""' in WORKER_PY
    # The note rides ALL THREE delivery texts the fallback note rides.
    assert WORKER_PY.count("+ fallback_note + trim_note") == 3


# ── BU-6: API serializers carry the flag ─────────────────────────

def test_bu_api_serializers_carry_reference_trimmed():
    assert '"reference_trimmed": bool(getattr(job, "reference_trimmed", False))' in API_PY
    assert '"reference_trimmed": _ref_trimmed,' in API_PY
    assert '_ref_trimmed = bool(getattr(j, "reference_trimmed", False))' in API_PY


# ── BU-7: honesty surfaces — locales + Mini App ──────────────────

def test_bu_bot_locales_have_trim_note_both_languages():
    for lang in ("my", "en"):
        note = LOCALES[lang].get("worker_ref_trim_note")
        assert note, f"{lang} missing worker_ref_trim_note"
        assert note.startswith("\n\n✂️")
        assert "45" in note and "50" in note


def test_bu_miniapp_i18n_keys_both_languages():
    assert I18N_JS.count("voice_ref_trim_notice:") == 2  # my + en
    assert I18N_JS.count("hist_ref_trim_badge:") == 2    # my + en


def test_bu_miniapp_surfaces_render_the_trim_note():
    # Result card: the fallback-note renderer carries the trim note too.
    renderer = APP_JS[
        APP_JS.index("function renderVoiceFallbackNote(data)"):
        APP_JS.index("function startJobPolling(jobId)")
    ]
    assert 'data.reference_trimmed' in renderer
    assert 't("voice_ref_trim_notice")' in renderer
    # Completion toasts mirror the fallback toast (both poll paths).
    assert APP_JS.count('if (data.reference_trimmed) { try { showStudioToast(t("voice_ref_trim_notice")') == 2
    # History detail note + history row badge.
    assert "if (!isRecap && j.reference_trimmed)" in APP_JS
    assert 't("hist_ref_trim_badge")' in APP_JS


def test_bu_cache_token_is_r92():
    assert "20261113_r141" in INDEX_HTML
    assert "20261104_r91" not in INDEX_HTML
