"""Unit tests for HF Ledger Truth — Round 1 (Space App v2 + Methods 1 & 2, 2026-11-27).

Covers Parts 1–8:
1. Space app v2 (`session_space/app.py`): exact declared-duration formulas, 2-tuple
   `((sr, wav), gpu_seconds_used)` return + 2 outputs on `/generate` and `/generate_session`,
   and `/probe_quota` `@spaces.GPU(duration=198)` touching 1 CUDA scalar without `load_model()`.
2. Deploy script (`scripts/deploy_voxcpm_spaces.py`): canary order, env-only write tokens,
   missing-token skip (exit 0), 180 s pause between uploads, one-time archive, `--rollback`,
   and 600 s `RUNNING` stage polling with zero secret leakage.
3. Engine result parser (`_resolve_result_and_gpu_seconds`): `str`, `dict`, 1-tuple rollback
   skew, valid 2-tuple, and invalid second elements (`None`, `NaN`, `inf`, negative, `bool`, `str`).
4. Method 1 success debit: `clamp(gpu_s + 1.0, 1.0, declared_door_s)` across session slices,
   legacy chunks, and recap TTS; fallbacks (`openbmb/VoxCPM-Demo` -> 0.0, session -> x1.0
   formula, legacy -> `space_gates` else 60.0); local WAV cache hits -> 0.0 debit.
5. Method 2 failure debits: mocked `status() -> None`/error -> 0.0 guardrail; queue-timeout
   reclassification (`"no gpu was available"`, `"zerogpu queue timeout"`); `"maximum allowed"`
   non-dead rule; Exits 4a/4b/5/6/7 debit math; `record_failure_debit` protection of
   `_refusal_rem_baseline`, `refused_gate_s`, `resets_at`, and 6-key token row schema.
6. Revealed-truth precedence (`record_admission_refusal`): bidirectional overwrite (up & down),
   `refusal_drift_deltas_s`, ceiling falsification when `clean_left >= existing_rg`,
   `"free ZeroGPU"` Pro-lapse `base_s` clamp to `300.0`, and `Try again in {wait}` parsing.
7. Ledger mutation safety: `botsetting_sync_mutate` `SELECT ... FOR UPDATE` transaction +
   mid-flight truth-timestamp causality guard (`truth_skip_debits`) across `aj_hf:truth_ts`.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import time
import types
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from app import worker as worker_mod
from app.config import settings
from app.services import dynamic_settings as ds_mod
from app.services import hf_pool_ledger as hf_ledger
from app.services import key_rotation as kr_mod
from app.services import voxcpm_engine as vox_mod
from app.services.recap_engine import core as recap_core
from app.services.recap_engine import voxcpm_engine as recap_vox_mod

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_wav(path: Path, *, hz: float = 150.0, duration_s: float = 2.5, sr: int = 24000) -> Path:
    n = max(1, int(round(duration_s * sr)))
    ts = np.arange(n, dtype=np.float64) / sr
    pcm = (0.5 * np.sin(2.0 * np.pi * hz * ts) * 16000.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return path


@pytest.fixture(autouse=True)
def _isolated_ledger(monkeypatch):
    kv: dict[str, str] = {}
    monkeypatch.setattr(kr_mod, "botsetting_sync_read", lambda k: kv.get(str(k)))
    monkeypatch.setattr(
        kr_mod,
        "botsetting_sync_write",
        lambda k, v: (kv.__setitem__(str(k), str(v)) is None) or True,
    )
    monkeypatch.setattr(
        kr_mod,
        "botsetting_sync_delete",
        lambda k: (kv.pop(str(k), None) is not None) or True,
    )
    monkeypatch.setattr(hf_ledger, "botsetting_sync_read", lambda k: kv.get(str(k)))
    monkeypatch.setattr(
        hf_ledger,
        "botsetting_sync_write",
        lambda k, v: (kv.__setitem__(str(k), str(v)) is None) or True,
    )
    monkeypatch.setattr(
        hf_ledger,
        "botsetting_sync_delete",
        lambda k: (kv.pop(str(k), None) is not None) or True,
    )
    hf_ledger.reset_ledger_for_tests(clear_store=True)
    vox_mod.VoxCPMEngine._token_cooldowns.clear()
    vox_mod.VoxCPMEngine._token_balances.clear()
    vox_mod.VoxCPMEngine._space_admission_prices.clear()
    vox_mod.VoxCPMEngine._last_admission_price = None
    vox_mod.VoxCPMEngine._token_rr_counter = 0
    vox_mod.VoxCPMEngine._token_bind_rr_counters.clear()
    yield kv
    hf_ledger.reset_ledger_for_tests(clear_store=True)


# ── Part 1: Space App v2 (`session_space/app.py`) ────────────────────────────


def test_part1_session_space_app_v2_endpoints_durations_and_probe_quota(monkeypatch):
    app_file = REPO_ROOT / "session_space" / "app.py"
    assert app_file.is_file()

    gpu_decorations: list[tuple[str, object]] = []
    registered_clicks: list[dict[str, object]] = []
    cuda_touches: list[tuple[object, object]] = []
    model_loads: list[int] = []

    fake_spaces = types.ModuleType("spaces")

    def _fake_gpu(fn=None, *, duration=None, **_kw):
        if fn is None:
            def _wrap(f):
                gpu_decorations.append((f.__name__, duration))
                return f
            return _wrap
        gpu_decorations.append((fn.__name__, duration))
        return fn

    fake_spaces.GPU = _fake_gpu

    class _Comp:
        def __init__(self, kind: str, **kw):
            self.kind = kind
            self.kw = kw

        def click(self, *, fn, inputs, outputs, api_name):
            registered_clicks.append(
                {"fn": fn, "inputs": list(inputs), "outputs": list(outputs), "api_name": api_name}
            )

    class _Ctx:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_gr = types.ModuleType("gradio")
    fake_gr.Blocks = _Ctx
    fake_gr.Row = _Ctx
    fake_gr.Column = _Ctx
    fake_gr.Markdown = lambda *a, **kw: _Comp("Markdown", **kw)
    fake_gr.Textbox = lambda *a, **kw: _Comp("Textbox", **kw)
    fake_gr.Audio = lambda *a, **kw: _Comp("Audio", **kw)
    fake_gr.Checkbox = lambda *a, **kw: _Comp("Checkbox", **kw)
    fake_gr.Slider = lambda *a, **kw: _Comp("Slider", **kw)
    fake_gr.Button = lambda *a, **kw: _Comp("Button", **kw)
    fake_gr.Number = lambda *a, **kw: _Comp("Number", **kw)

    class _FakeTensor:
        def sum(self):
            return self

        def item(self):
            return 1.0

    fake_torch = types.ModuleType("torch")
    fake_torch.compile = lambda m, *a, **k: m
    fake_torch.Tensor = _FakeTensor
    fake_torch.float32 = np.float32
    fake_torch.from_numpy = lambda arr: arr
    fake_torch.inference_mode = _Ctx
    fake_torch.cuda = SimpleNamespace(is_available=lambda: False, empty_cache=lambda: None)
    fake_torch.ones = lambda n, device=None: (cuda_touches.append((n, device)) or _FakeTensor())

    fake_sf = types.ModuleType("soundfile")
    fake_sf.read = lambda _p: (np.zeros(240, dtype=np.float32), 24000)
    fake_ta = types.ModuleType("torchaudio")
    fake_ta.load = None

    fake_voxcpm = types.ModuleType("voxcpm")

    class _FakeVoxModel:
        tts_model = SimpleNamespace(sample_rate=24000)

        def generate(self, **_kw):
            return np.ones(480, dtype=np.float32) * 0.5

    class _FakeVoxCPM:
        @staticmethod
        def from_pretrained(*_a, **_kw):
            model_loads.append(1)
            return _FakeVoxModel()

    fake_voxcpm.VoxCPM = _FakeVoxCPM

    monkeypatch.setitem(sys.modules, "spaces", fake_spaces)
    monkeypatch.setitem(sys.modules, "gradio", fake_gr)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "soundfile", fake_sf)
    monkeypatch.setitem(sys.modules, "torchaudio", fake_ta)
    monkeypatch.setitem(sys.modules, "voxcpm", fake_voxcpm)

    spec = importlib.util.spec_from_file_location("vcp_session_space_app", app_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 1. Exact declared-duration formulas untouched
    for chars in (0, 20, 100, 250, 850, 1500):
        txt = "က" * chars
        assert mod._dyn_duration(text_input=txt) == min(55, max(25, 18 + int(chars * 0.15)))
        assert mod._session_duration(text_input=txt) == min(120, max(30, 18 + int(chars * 0.085)))

    # 2. `/probe_quota` decorated with duration=198, touches 1 CUDA scalar, NEVER calls `load_model()`!
    by_fn = dict(gpu_decorations)
    assert by_fn["probe_quota"] == 198
    assert by_fn["generate"] is mod._dyn_duration
    assert by_fn["generate_session"] is mod._session_duration

    model_loads.clear()
    cuda_touches.clear()
    probe_val = mod.probe_quota()
    assert isinstance(probe_val, float) and probe_val >= 0.0
    assert cuda_touches == [(1, "cuda")]
    assert model_loads == []  # `load_model()` was NOT called!

    # 3. `generate` and `generate_session` return `((sr, wav), gpu_seconds_used)`
    res_gen = mod.generate("မင်္ဂလာပါ။", "", None, False, "", 2.0, True, False)
    res_sess = mod.generate_session("မင်္ဂလာပါ ခင်ဗျာ။", "", None, False, "", 2.0, True, False)
    for res in (res_gen, res_sess):
        assert isinstance(res, tuple) and len(res) == 2
        audio_pair, gpu_s = res
        assert isinstance(audio_pair, tuple) and len(audio_pair) == 2
        sr, wav = audio_pair
        assert sr == 24000 and isinstance(wav, np.ndarray) and wav.size > 0
        assert isinstance(gpu_s, float) and gpu_s >= 0.0

    # 4. Endpoint bindings: `generate` & `generate_session` have 2 outputs `[Audio, Number]`,
    #    `probe_quota` has 1 `Number` output
    clicks_by_api = {c["api_name"]: c for c in registered_clicks}
    assert set(clicks_by_api.keys()) == {"generate", "generate_session", "probe_quota"}
    for api_nm in ("generate", "generate_session"):
        outs = clicks_by_api[api_nm]["outputs"]
        assert len(outs) == 2
        assert outs[0].kind == "Audio"
        assert outs[1].kind == "Number" and outs[1].kw.get("visible") is False
    probe_outs = clicks_by_api["probe_quota"]["outputs"]
    assert len(probe_outs) == 1 and probe_outs[0].kind == "Number"


# ── Part 2: Deploy Script (`scripts/deploy_voxcpm_spaces.py`) ────────────────


def test_part2_deploy_script_canary_order_skip_archive_rollback_and_poll(tmp_path):
    from scripts import deploy_voxcpm_spaces as dep

    assert dep.CANARY_SPACES == (
        "yunaisbusy/voxcpm2-9",
        "yunaisbusy/voxcpm2-8",
        "awints001/voxcpm2-b",
        "oneclick-org/voxcpm2-b",
        "oneclick-org/voxcpm2-a",
        "awints001/voxcpm2-a",
    )
    assert dep.ACCOUNT_TOKEN_ENV == {
        "yunaisbusy": "HF_WRITE_TOKEN_YUNAISBUSY",
        "awints001": "HF_WRITE_TOKEN_AWINTS001",
        "oneclick-org": "HF_WRITE_TOKEN_ONECLICK_ORG",
    }

    app_v2 = tmp_path / "app.py"
    app_v2.write_text("# v2 space app\n", encoding="utf-8")
    arch_dir = tmp_path / "archive"
    live_v1 = tmp_path / "live_v1.py"
    live_v1.write_text("# v1 archived live app\n", encoding="utf-8")

    uploads: list[tuple[str, str, str]] = []
    sleeps: list[float] = []
    logs: list[str] = []
    stages_by_space: dict[str, list[str]] = {
        "yunaisbusy/voxcpm2-9": ["BUILDING", "RUNNING"],
        "yunaisbusy/voxcpm2-8": ["RUNNING"],
    }

    class _FakeApi:
        def __init__(self, token: str):
            self.token = token

        def hf_hub_download(self, *, repo_id: str, filename: str, repo_type: str) -> str:
            assert filename == "app.py" and repo_type == "space"
            return str(live_v1)

        def upload_file(self, *, path_or_fileobj: str, path_in_repo: str, repo_id: str, repo_type: str):
            assert path_in_repo == "app.py" and repo_type == "space"
            uploads.append((repo_id, Path(path_or_fileobj).read_text(encoding="utf-8"), self.token))

        def get_space_runtime(self, *, repo_id: str):
            seq = stages_by_space.setdefault(repo_id, ["RUNNING"])
            st = seq.pop(0) if len(seq) > 1 else seq[0]
            return SimpleNamespace(stage=st)

    # Partial rollout with ONLY `HF_WRITE_TOKEN_YUNAISBUSY` set:
    # deploys the 2 yunaisbusy Spaces (with 180s pause between them) and SKIPS the other 4 (exit 0)!
    secret_tok = "hf_SUPER_SECRET_YUNA_WRITE"
    env = {"HF_WRITE_TOKEN_YUNAISBUSY": secret_tok}
    rc = dep.main(
        ["--all"],
        app_path=app_v2,
        archive_dir=arch_dir,
        environ=env,
        api_factory=_FakeApi,
        pause_s=180.0,
        poll_interval_s=10.0,
        sleep_fn=sleeps.append,
        out_fn=logs.append,
    )
    assert rc == 0
    assert [sp for sp, _body, _tok in uploads] == ["yunaisbusy/voxcpm2-9", "yunaisbusy/voxcpm2-8"]
    assert all(body == "# v2 space app\n" for _sp, body, _t in uploads)
    # 10s poll sleep on voxcpm2-9 BUILDING->RUNNING + exactly ONE 180s inter-upload pause (none after skips!)
    assert sleeps.count(180.0) == 1
    assert any("SKIP awints001/voxcpm2-b" in ln for ln in logs)
    assert any("SKIP oneclick-org/voxcpm2-a" in ln for ln in logs)
    assert all(secret_tok not in ln for ln in logs)  # zero secret leak!

    # Archive created once and preserved on re-deploy; `--rollback --space` restores v1!
    arch_9 = arch_dir / "yunaisbusy_voxcpm2-9_app.py"
    assert arch_9.read_text(encoding="utf-8") == "# v1 archived live app\n"
    live_v1.write_text("# mutated later\n", encoding="utf-8")
    dep.main(
        ["--space", "yunaisbusy/voxcpm2-9"],
        app_path=app_v2,
        archive_dir=arch_dir,
        environ=env,
        api_factory=_FakeApi,
        sleep_fn=lambda _s: None,
        out_fn=logs.append,
    )
    assert arch_9.read_text(encoding="utf-8") == "# v1 archived live app\n"

    uploads.clear()
    rc_rb = dep.main(
        ["--rollback", "--space", "yunaisbusy/voxcpm2-9"],
        app_path=app_v2,
        archive_dir=arch_dir,
        environ=env,
        api_factory=_FakeApi,
        sleep_fn=lambda _s: None,
        out_fn=logs.append,
    )
    assert rc_rb == 0
    assert uploads == [("yunaisbusy/voxcpm2-9", "# v1 archived live app\n", secret_tok)]


# ── Part 3: Engine Result & GPU-Seconds Parser ───────────────────────────────


def test_part3_resolve_result_and_gpu_seconds_handles_all_shapes_and_skew():
    parse = vox_mod._resolve_result_and_gpu_seconds
    assert parse(None) == (None, None)
    assert parse("/tmp/a.wav") == ("/tmp/a.wav", None)
    assert parse({"path": "/tmp/b.wav"}) == ("/tmp/b.wav", None)
    # 1-tuple rollback skew: never indexes `[1]`!
    assert parse(("/tmp/c.wav",)) == ("/tmp/c.wav", None)
    assert parse([{"url": "/tmp/d.wav"}]) == ("/tmp/d.wav", None)
    # Valid 2-tuples
    assert parse(("/tmp/e.wav", 24.75)) == ("/tmp/e.wav", 24.75)
    assert parse([{"path": "/tmp/f.wav"}, 0]) == ("/tmp/f.wav", 0.0)
    # Invalid second elements safely yield `gpu_s = None` while keeping path
    for bad in (None, "12.3", -0.1, float("nan"), float("inf"), float("-inf"), True, False, {}):
        assert parse(("/tmp/g.wav", bad)) == ("/tmp/g.wav", None)


# ── Part 4: Method 1 Success Debit, Clamp, Fallbacks, & Cache Hit ────────────


def test_part4_method1_success_debit_clamp_fallbacks_and_cache_hit(tmp_path, monkeypatch):
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )
    gen_wav = _make_wav(tmp_path / "ok.wav")

    # Unit-check clamp math: `clamp(gpu_s + 1.0, 1.0, declared_door_s)`
    # Session 849 chars -> door 137.0s
    assert hf_ledger.compute_method1_debit_s(31.4, session_mode=True, chars=849) == 32.4
    assert hf_ledger.compute_method1_debit_s(0.0, session_mode=True, chars=849) == 1.0
    assert hf_ledger.compute_method1_debit_s(220.0, session_mode=True, chars=849) == 137.0
    # Legacy 100 chars -> declared `min(55, max(25, 18 + 15)) = 33.0`, door `33 * 1.5 = 49.5s`
    assert hf_ledger.legacy_declared_s(100) == 33.0
    assert hf_ledger.legacy_declared_door_s(100) == 49.5
    assert hf_ledger.compute_method1_debit_s(18.25, session_mode=False, chars=100) == 19.25
    assert hf_ledger.compute_method1_debit_s(90.0, session_mode=False, chars=100) == 49.5

    sp = "awints001/voxcpm2-a"
    tok = "hf_m1_tok"
    now = time.time()
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok): {
                    "status": "ok",
                    "remaining_s": 300.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": "",
                }
            },
            "space_gates": {sp: 83},  # inflated legacy gate ignored when `gpu_s` is present!
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )

    ret_box: dict[str, object] = {"result": (str(gen_wav), 27.5)}

    class _M1Client:
        def submit(self, *a, **kw):
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: ret_box["result"])

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, _sp, hf_token=None: _M1Client()),
    )

    eng = vox_mod.VoxCPMEngine(work_dir=tmp_path / "w_m1", spaces=[sp], hf_tokens=[tok])

    # 1. Session slice with `gpu_s = 27.5` -> debits `28.5s` (NOT 91.0s formula!)
    eng.synthesize("က" * 849, tmp_path / "s1.wav", skip_mastering=True, allow_fallback=False, session_mode=True)
    assert hf_ledger.get_token_remaining_s(tok) == pytest.approx(300.0 - 28.5)

    # 2. Legacy/recap call with `gpu_s = 16.0` -> debits `17.0s` (NOT 83s `space_gates`!)
    ret_box["result"] = (str(gen_wav), 16.0)
    eng.synthesize("ခ" * 100, tmp_path / "l1.wav", skip_mastering=True, allow_fallback=False, session_mode=False)
    assert hf_ledger.get_token_remaining_s(tok) == pytest.approx(300.0 - 28.5 - 17.0)

    # 3. Fallback when `gpu_s is None`: legacy uses `space_gates[sp] == 83`, session uses `session_call_gate_s(59) == 24`
    ret_box["result"] = str(gen_wav)
    eng.synthesize("ဂ" * 100, tmp_path / "l2.wav", skip_mastering=True, allow_fallback=False, session_mode=False)
    assert hf_ledger.get_token_remaining_s(tok) == pytest.approx(300.0 - 28.5 - 17.0 - 83.0)

    eng.synthesize("ဃ" * 59, tmp_path / "s2.wav", skip_mastering=True, allow_fallback=False, session_mode=True)
    assert hf_ledger.get_token_remaining_s(tok) == pytest.approx(300.0 - 28.5 - 17.0 - 83.0 - 24.0)

    # 4. `openbmb/VoxCPM-Demo` fallback -> 0.0 debit
    before_demo = hf_ledger.get_token_remaining_s(tok)
    hf_ledger.record_success_debit("openbmb/VoxCPM-Demo", tok)
    vox_mod.VoxCPMEngine._record_success_balance("openbmb/VoxCPM-Demo", tok)
    assert hf_ledger.get_token_remaining_s(tok) == pytest.approx(before_demo)

    # 5. Local WAV cache hit (`skip_mastering=False`, same text) -> 0.0 debit on second call!
    ret_box["result"] = (str(gen_wav), 14.0)
    eng.synthesize("င" * 80, tmp_path / "c1.wav", skip_mastering=False, allow_fallback=False)
    after_first = hf_ledger.get_token_remaining_s(tok)
    eng.synthesize("င" * 80, tmp_path / "c2.wav", skip_mastering=False, allow_fallback=False)
    assert hf_ledger.get_token_remaining_s(tok) == pytest.approx(after_first)


# ── Part 5: Method 2 Failure Debits, Queue-Timeout Fix, & Guardrails ─────────


def test_part5_queue_timeout_reclassification_and_maximum_allowed_non_dead(tmp_path, monkeypatch):
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    monkeypatch.setattr(vox_mod.time, "sleep", lambda _s: None)
    gen_wav = _make_wav(tmp_path / "ok5.wav")

    # 1. `"no gpu was available"` and `"zerogpu queue timeout"` are congestion/timeout, NOT quota-dead!
    for msg in (
        "AppError: ZeroGPU queue timeout: No GPU was available after 60s. Subscribe to Hugging Face PRO",
        "ZeroGPU client error: No GPU was available",
    ):
        assert vox_mod._quota_failure_kind(msg) is None
        exc = RuntimeError(msg)
        assert vox_mod._is_pair_space_timeout(exc) is True
        assert vox_mod.is_voxcpm_capacity_or_congestion_error(exc) is True

    sp_busy = "awints001/voxcpm2-busy"
    sp_ok = "awints001/voxcpm2-ok"
    tok = "hf_tok_qto"
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok): {
                    "status": "ok",
                    "remaining_s": 250.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": "",
                }
            },
            "space_gates": {sp_busy: 60, sp_ok: 60},
            "in_flight_gpu_s": 0,
        }
    )

    class _BusyThenOkClient:
        def __init__(self, sp: str):
            self.sp = sp

        def submit(self, *a, **kw):
            if self.sp == sp_busy:
                raise RuntimeError("ZeroGPU queue timeout: No GPU was available after 60s")
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: (str(gen_wav), 10.0))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _BusyThenOkClient(sp)),
    )
    eng = vox_mod.VoxCPMEngine(work_dir=tmp_path / "w_qto", spaces=[sp_busy, sp_ok], hf_tokens=[tok])
    eng.synthesize("စမ်းသပ်။", tmp_path / "qto.wav", skip_mastering=True, allow_fallback=False)
    assert eng.quota_refusals == 0
    assert hf_ledger.is_space_cooling(sp_busy) is True
    assert eng.job_memory.token_dead(tok) is False
    assert hf_ledger.get_token_remaining_s(tok) == pytest.approx(250.0 - 11.0)

    # 2. Exit 2 `"maximum allowed"` (`ZeroGPUIllegalDuration`) sets ceiling, 0 debit, does NOT dead-mark token!
    tok_max = "hf_tok_illegal_dur"
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_max): {
                    "status": "ok",
                    "remaining_s": 280.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": "",
                }
            },
            "space_gates": {sp_ok: 60},
            "in_flight_gpu_s": 0,
        }
    )

    class _IllegalDurClient:
        def submit(self, *a, **kw):
            raise RuntimeError(
                "The requested GPU duration (301s) is larger than the maximum allowed. "
                "Subscribe to Hugging Face PRO to allow GPU tasks up to 40 min"
            )

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _IllegalDurClient()),
    )
    eng_il = vox_mod.VoxCPMEngine(work_dir=tmp_path / "w_il", spaces=[sp_ok], hf_tokens=[tok_max])
    with pytest.raises(vox_mod.VoxCPMQuotaExhaustedError):
        eng_il.synthesize("တိုတို။", tmp_path / "il.wav", skip_mastering=True, allow_fallback=False)
    assert eng_il.job_memory.token_dead(tok_max) is False
    assert tok_max not in vox_mod.VoxCPMEngine._token_cooldowns
    assert hf_ledger.get_token_remaining_s(tok_max) == pytest.approx(280.0)
    assert hf_ledger.get_token_refused_gate_s(tok_max) == pytest.approx(60.0)


def test_part5_failure_debit_class_table_mocked_status_guardrail_and_baseline_protection(
    tmp_path, monkeypatch
):
    import gradio_client
    from gradio_client.exceptions import AppError

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    monkeypatch.setattr(vox_mod.time, "sleep", lambda _s: None)
    empty_wav = tmp_path / "tiny.wav"
    empty_wav.write_bytes(b"RIFF0000")  # <= 1000 bytes -> Exit 6 malformed/empty!

    sp = "awints001/voxcpm2-a"
    tok = "hf_m2_tok"
    d_key = hf_ledger.token_digest(tok)

    # 1. `record_failure_debit` invariants: <= 0 is no-op; never touches baseline, ceiling, resets_at, or 6-key shape
    t0 = 1_796_000_000.0
    rst_iso = datetime.fromtimestamp(t0 + 40000, tz=timezone.utc).isoformat()
    hf_ledger.save_pool_state(
        {
            "tokens": {
                d_key: {
                    "status": "ok",
                    "remaining_s": 200.0,
                    "base_s": 300.0,
                    "resets_at": rst_iso,
                    "cooldown_until": "",
                    "updated_at": "",
                }
            },
            "space_gates": {sp: 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=t0,
    )
    assert hf_ledger.record_failure_debit(sp, tok, 0.0, now_ts=t0) is None
    assert hf_ledger.record_failure_debit(sp, tok, -5.0, now_ts=t0) is None
    st_plain = hf_ledger.record_failure_debit(sp, tok, 15.0, now_ts=t0 + 10)
    assert st_plain is not None
    assert set(st_plain["tokens"][d_key].keys()) == {
        "status",
        "remaining_s",
        "base_s",
        "resets_at",
        "cooldown_until",
        "updated_at",
    }
    assert st_plain["tokens"][d_key]["remaining_s"] == pytest.approx(185.0)
    assert st_plain["tokens"][d_key]["resets_at"] == rst_iso

    # Now add ceiling (baseline=185.0, ceiling=137.0) and verify failure debit NEVER lowers `_refusal_rem_baseline`
    hf_ledger.record_refusal_ceiling(tok, 137.0, now_ts=t0 + 20)
    assert hf_ledger._refusal_rem_baseline[d_key] == pytest.approx(185.0)
    hf_ledger.record_failure_debit(sp, tok, 40.0, now_ts=t0 + 30)
    assert hf_ledger.get_token_remaining_s(tok, now_ts=t0 + 30) == pytest.approx(145.0)
    assert hf_ledger._refusal_rem_baseline[d_key] == pytest.approx(185.0)
    assert hf_ledger.get_token_refused_gate_s(tok, now_ts=t0 + 30) == pytest.approx(137.0)

    # 2. End-to-end engine failure classes with controllable fake clock & `status()`
    clock = {"t": 2_000_000.0}
    monkeypatch.setattr(vox_mod.time, "time", lambda: clock["t"])

    def _reset_tok(rem: float = 250.0) -> None:
        hf_ledger.save_pool_state(
            {
                "tokens": {
                    d_key: {
                        "status": "ok",
                        "remaining_s": rem,
                        "base_s": 300.0,
                        "resets_at": rst_iso,
                        "cooldown_until": "",
                        "updated_at": "",
                    }
                },
                "space_gates": {sp: 60},
                "in_flight_gpu_s": 0,
            },
            now_ts=clock["t"],
        )
        vox_mod.VoxCPMEngine._token_balances.clear()

    # For 100-char legacy `/generate`: `declared_s = 33.0`, `declared_door_s = 49.5`
    text_100 = "က" * 100

    def _run_attempt(job_factory, **synth_kw):
        monkeypatch.setattr(
            vox_mod.VoxCPMEngine,
            "get_client",
            classmethod(lambda cls, _sp, hf_token=None: SimpleNamespace(submit=lambda *a, **k: job_factory())),
        )
        eng = vox_mod.VoxCPMEngine(work_dir=tmp_path / "w_m2", spaces=[sp], hf_tokens=[tok])
        with pytest.raises(Exception):
            eng.synthesize(text_100, tmp_path / "fail.wav", skip_mastering=True, allow_fallback=False, **synth_kw)

    # (A) Guardrail: mocked `status() -> None` or `status()` raising -> 0.0 debit across ALL failure classes!
    for bad_status in (lambda: None, lambda: (_ for _ in ()).throw(RuntimeError("mock status err"))):
        _reset_tok(250.0)

        def _job_no_status(_st=bad_status):
            calls = {"n": 0}

            def _done():
                calls["n"] += 1
                if calls["n"] == 1:
                    return False
                clock["t"] += 20.0
                return True

            return SimpleNamespace(done=_done, status=_st, result=lambda: (_ for _ in ()).throw(AppError("CUDA OOM")))

        _run_attempt(_job_no_status)
        assert hf_ledger.get_token_remaining_s(tok, now_ts=clock["t"]) == pytest.approx(250.0)

    # (B) Exit 4a Timeout: `< 5.0 s` in PROCESSING -> 0.0; `>= 5.0 s` (14.0 s) -> `min(33.0, 14.0) = 14.0`
    _reset_tok(250.0)

    def _job_to_short():
        step = {"n": 0}

        def _done():
            step["n"] += 1
            if step["n"] == 2:
                clock["t"] += 46.0  # triggers 45s cap, but PROCESSING only started 3s before timeout!
            return False

        def _status():
            if step["n"] == 1:
                clock["t"] += 43.0
                return SimpleNamespace(code="PROCESSING")
            return SimpleNamespace(code="PROCESSING")

        return SimpleNamespace(done=_done, status=_status, cancel=lambda: None, result=lambda: "")

    # Wait: in `_job_to_short`, `t_proc_start` is stamped when `_status()` returns `PROCESSING` at `t + 43`,
    # and on step 2 `clock["t"]` advances by `+3.0` (`t + 46 > t + 45` cap) -> `gpu_elapsed_s == 3.0 < 5.0`!
    def _job_to_3s():
        step = {"n": 0}

        def _done():
            step["n"] += 1
            if step["n"] == 2:
                clock["t"] += 3.0  # total 46s > 45s cap, gpu_elapsed_s == 3.0s < 5.0s!
            return False

        def _status():
            clock["t"] += 43.0
            return SimpleNamespace(code="PROCESSING")

        return SimpleNamespace(done=_done, status=_status, cancel=lambda: None, result=lambda: "")

    _run_attempt(_job_to_3s)
    assert hf_ledger.get_token_remaining_s(tok, now_ts=clock["t"]) == pytest.approx(250.0)

    _reset_tok(250.0)

    def _job_to_14s():
        step = {"n": 0}

        def _done():
            step["n"] += 1
            if step["n"] == 2:
                clock["t"] += 14.0  # total 46s > 45s cap, gpu_elapsed_s == 14.0s >= 5.0s!
            return False

        def _status():
            clock["t"] += 32.0
            return SimpleNamespace(code="PROCESSING")

        return SimpleNamespace(done=_done, status=_status, cancel=lambda: None, result=lambda: "")

    _run_attempt(_job_to_14s)
    assert hf_ledger.get_token_remaining_s(tok, now_ts=clock["t"]) == pytest.approx(250.0 - 14.0)

    # (C) Exit 5 Transport error (`HTTP 502 Bad Gateway`): `< 5.0 s` -> 0.0; `12.0 s` -> 12.0
    _reset_tok(250.0)

    def _job_502_short():
        return SimpleNamespace(
            done=lambda: True,
            status=lambda: SimpleNamespace(code="PROCESSING"),
            result=lambda: (clock.__setitem__("t", clock["t"] + 2.0), (_ for _ in ()).throw(RuntimeError("HTTP 502 Bad Gateway")))[1],
        )

    _run_attempt(_job_502_short)
    assert hf_ledger.get_token_remaining_s(tok, now_ts=clock["t"]) == pytest.approx(250.0)

    _reset_tok(250.0)

    def _job_502_12s():
        return SimpleNamespace(
            done=lambda: True,
            status=lambda: SimpleNamespace(code="PROCESSING"),
            result=lambda: (clock.__setitem__("t", clock["t"] + 12.0), (_ for _ in ()).throw(ConnectionResetError("peer reset")))[1],
        )

    _run_attempt(_job_502_12s)
    assert hf_ledger.get_token_remaining_s(tok, now_ts=clock["t"]) == pytest.approx(250.0 - 12.0)

    # (D) Exit 6 Malformed/empty WAV: tuple `(empty_wav, 9.5)` -> `10.5s`; bare `empty_wav` + `PROCESSING` (0.5s) -> `2.0s` floor
    _reset_tok(250.0)
    _run_attempt(
        lambda: SimpleNamespace(
            done=lambda: True,
            status=lambda: None,
            result=lambda: (str(empty_wav), 9.5),
        )
    )
    assert hf_ledger.get_token_remaining_s(tok, now_ts=clock["t"]) == pytest.approx(250.0 - 10.5)

    _reset_tok(250.0)
    _run_attempt(
        lambda: SimpleNamespace(
            done=lambda: True,
            status=lambda: SimpleNamespace(code="PROCESSING"),
            result=lambda: (clock.__setitem__("t", clock["t"] + 0.5) or str(empty_wav)),
        )
    )
    assert hf_ledger.get_token_remaining_s(tok, now_ts=clock["t"]) == pytest.approx(250.0 - 2.0)

    # (E) Exit 7 Space-side exception (`AppError`): `< 3.0 s` wall & gpu -> 0.0; `60.0 s` -> capped at `declared_door_s == 49.5s`!
    _reset_tok(250.0)
    _run_attempt(
        lambda: SimpleNamespace(
            done=lambda: True,
            status=lambda: SimpleNamespace(code="PROCESSING"),
            result=lambda: (clock.__setitem__("t", clock["t"] + 1.5), (_ for _ in ()).throw(AppError("Bad audio")))[1],
        )
    )
    assert hf_ledger.get_token_remaining_s(tok, now_ts=clock["t"]) == pytest.approx(250.0)

    _reset_tok(250.0)
    _run_attempt(
        lambda: SimpleNamespace(
            done=lambda: True,
            status=lambda: SimpleNamespace(code="ITERATING"),
            result=lambda: (clock.__setitem__("t", clock["t"] + 60.0), (_ for _ in ()).throw(AppError("CUDA OOM")))[1],
        )
    )
    assert hf_ledger.get_token_remaining_s(tok, now_ts=clock["t"]) == pytest.approx(250.0 - 49.5)


# ── Part 6: Revealed-Truth Precedence, Ceiling Falsification, & Wait Parser ──


def test_part6_revealed_truth_overwrite_both_directions_ceiling_falsification_pro_clamp_and_wait_parser(
    tmp_path, monkeypatch
):
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    gen_wav = _make_wav(tmp_path / "ok6.wav")

    # 1. `Try again in {wait}` parser across `spaces/zero/client.py` timedelta & human unit formats
    assert hf_ledger.parse_refusal_retry_wait_s("Try again in 0:05:12. Subscribe") == pytest.approx(312.0)
    assert hf_ledger.parse_refusal_retry_wait_s("Try again in 1 day, 0:00:30.") == pytest.approx(86430.0)
    assert hf_ledger.parse_refusal_retry_wait_s("Try again in 02:30:00.500000") == pytest.approx(9000.5)
    assert hf_ledger.parse_refusal_retry_wait_s("Try again in 45s") == pytest.approx(45.0)
    assert hf_ledger.parse_refusal_retry_wait_s("Try again in 2h 15m 10s.") == pytest.approx(8110.0)
    assert hf_ledger.parse_refusal_retry_wait_s("Try again in 12 minutes") == pytest.approx(720.0)
    assert hf_ledger.parse_refusal_retry_wait_s("No wait string here") is None

    sp = "awints001/voxcpm2-a"
    tok_down = "hf_truth_down"
    tok_up = "hf_truth_up_pro_lapsed"
    tok_ok = "hf_truth_ok"
    d_down = hf_ledger.token_digest(tok_down)
    d_up = hf_ledger.token_digest(tok_up)
    t0 = 1_797_000_000.0
    expired_rst = datetime.fromtimestamp(t0 - 500, tz=timezone.utc).isoformat()

    # Seed:
    # - `tok_down`: ledger=210s (overestimated), expired `resets_at` -> refused with 80s left (drift = +130.0)
    # - `tok_up`: ledger=35s (underestimated), `base_s=2400.0` (lapsed Pro!), active `refused_gate_s=60.0`
    #             -> refused at 137s session door with `"free ZeroGPU quota (137s requested vs. 95s left). Try again in 0:10:00"`
    #             -> overwrites UP to 95.0 (drift = -60.0), clamps `base_s` to 300.0, sets `resets_at = t0 + 600`,
    #                AND falsifies `refused_gate_s` from 60.0 -> 137.0!
    hf_ledger.save_pool_state(
        {
            "tokens": {
                d_down: {
                    "status": "ok",
                    "remaining_s": 210.0,
                    "base_s": 300.0,
                    "resets_at": expired_rst,
                    "cooldown_until": "",
                    "updated_at": "",
                },
                d_up: {
                    "status": "ok",
                    "remaining_s": 35.0,
                    "base_s": 2400.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": "",
                },
                hf_ledger.token_digest(tok_ok): {
                    "status": "ok",
                    "remaining_s": 300.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": "",
                },
            },
            "space_gates": {sp: 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=t0,
    )
    hf_ledger.record_refusal_ceiling(tok_up, 60.0, now_ts=t0)
    assert hf_ledger.get_token_refused_gate_s(tok_up, now_ts=t0) == 60.0

    # Direct `record_admission_refusal` check on `tok_up` (since walk skips <137s pre-walk unless tested on a higher unknown door or direct call)
    drift_up: list[float] = []
    st_up = hf_ledger.record_admission_refusal(
        sp,
        tok_up,
        137.0,
        95.0,
        update_space_gate=False,
        session_mode=True,
        session_chars=849,
        error_text=(
            "You have exceeded your free ZeroGPU quota (137s requested vs. 95s left). "
            "Try again in 0:10:00. Subscribe to Hugging Face PRO"
        ),
        drift_out=drift_up,
        now_ts=t0 + 50,
    )
    row_up = st_up["tokens"][d_up]
    assert drift_up == [-60.0]  # 35.0 - 95.0 = -60.0 (signed!)
    assert row_up["remaining_s"] == pytest.approx(95.0)
    assert row_up["base_s"] == pytest.approx(300.0)  # clamped from 2400.0 -> 300.0!
    assert row_up["refused_gate_s"] == pytest.approx(137.0)  # falsified 60.0 -> 137.0!
    assert hf_ledger._parse_ts(row_up["resets_at"]) == pytest.approx(t0 + 50 + 600.0)
    assert hf_ledger.get_token_last_truth_ts(tok_up) == pytest.approx(t0 + 50)
    # Schema pin: no extra keys leaked into `aj_hf:pool_state` token row, and `aj_hf:truth_ts` is outside `CONFIG_SETTING_KEYS`!
    assert set(row_up.keys()) == {
        "status",
        "remaining_s",
        "base_s",
        "resets_at",
        "cooldown_until",
        "updated_at",
        "refused_gate_s",
        "refused_until",
    }
    assert hf_ledger.TRUTH_TS_STORE_KEY not in ds_mod.CONFIG_SETTING_KEYS

    # Engine walk check on `tok_down`: ledger=210s -> refused `(137s requested vs. 80s left)` (no wait text -> expired reset advances +86400s!)
    monkeypatch.setattr(vox_mod.time, "time", lambda: t0 + 100)

    class _DownClient:
        def __init__(self, tok: str | None):
            self.tok = tok

        def submit(self, *a, **kw):
            if self.tok == tok_down:
                raise RuntimeError("You have exceeded your free ZeroGPU quota (137s requested vs. 80s left)")
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: (str(gen_wav), 20.0))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, _sp, hf_token=None: _DownClient(hf_token)),
    )
    eng = vox_mod.VoxCPMEngine(work_dir=tmp_path / "w_p6", spaces=[sp], hf_tokens=[tok_down, tok_ok])
    eng.synthesize("က" * 849, tmp_path / "p6.wav", skip_mastering=True, allow_fallback=False, session_mode=True)
    assert eng.refusal_drift_deltas_s == [130.0]  # 210.0 - 80.0 == +130.0
    assert vox_mod.VoxCPMEngine._token_balances[tok_down] == pytest.approx(80.0)
    st_after = hf_ledger.load_pool_state(now_ts=t0 + 100)
    assert st_after["tokens"][d_down]["remaining_s"] == pytest.approx(80.0)
    assert hf_ledger._parse_ts(st_after["tokens"][d_down]["resets_at"]) == pytest.approx(t0 + 100 + 86400.0)


# ── Part 7: `botsetting_sync_mutate` FOR UPDATE & Causality Skip Guard ───────


def test_part7_botsetting_sync_mutate_for_update_sql_and_causality_skip(
    tmp_path, monkeypatch, _isolated_ledger, caplog
):
    # 1. Direct verification of `botsetting_sync_mutate` PostgreSQL `SELECT ... FOR UPDATE` inside single transaction
    executed_sql: list[tuple[str, dict]] = []
    stored_val = {"v": '{"count": 1}'}

    class _FakeConn:
        def execute(self, stmt, params=None):
            sql_s = str(stmt)
            executed_sql.append((sql_s, dict(params or {})))
            if sql_s.strip().upper().startswith("SELECT"):
                return SimpleNamespace(first=lambda: (stored_val["v"],) if stored_val["v"] is not None else None)
            if "INSERT INTO" in sql_s.upper():
                stored_val["v"] = (params or {}).get("v")
            return SimpleNamespace(first=lambda: None)

    class _FakePgEngine:
        dialect = SimpleNamespace(name="postgresql")

        @contextmanager
        def begin(self):
            yield _FakeConn()

    # Temporarily restore original sync read/write references so `botsetting_sync_mutate` uses `_sync_engine()`
    monkeypatch.setattr(kr_mod, "botsetting_sync_read", kr_mod._ORIG_BOTSETTING_SYNC_READ)
    monkeypatch.setattr(kr_mod, "botsetting_sync_write", kr_mod._ORIG_BOTSETTING_SYNC_WRITE)
    monkeypatch.setattr(kr_mod, "_sync_engine", lambda: _FakePgEngine())

    ok = kr_mod.botsetting_sync_mutate(
        "aj_hf:pool_state",
        lambda cur: json.dumps({"count": json.loads(cur or "{}").get("count", 0) + 1}),
    )
    assert ok is True
    assert stored_val["v"] == '{"count": 2}'
    assert len(executed_sql) == 2
    assert "FOR UPDATE" in executed_sql[0][0].upper()
    assert "INSERT INTO BOT_SETTINGS" in executed_sql[1][0].upper()

    # Restore isolated KV store for end-to-end causality skip verification
    kv = _isolated_ledger
    monkeypatch.setattr(kr_mod, "botsetting_sync_read", lambda k: kv.get(str(k)))
    monkeypatch.setattr(
        kr_mod,
        "botsetting_sync_write",
        lambda k, v: (kv.__setitem__(str(k), str(v)) is None) or True,
    )

    # 2. Mid-flight causality guard:
    # Worker A starts attempt on `tok_race` at `t = 1000.0`;
    # Worker B hits a numeric refusal (`left = 110.0`) at `t = 1005.0` while Worker A is in flight;
    # Worker A finishes at `t = 1020.0` -> BOTH success debit AND failure debit skip subtraction (`truth_skip_debits == 1`)!
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    monkeypatch.setattr(settings, "clone_session_slices_enabled", True)
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: False)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )
    gen_wav = _make_wav(tmp_path / "race.wav")

    sp = "awints001/voxcpm2-a"
    tok_race = "hf_tok_midflight_race"
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_race): {
                    "status": "ok",
                    "remaining_s": 200.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": "",
                }
            },
            "space_gates": {sp: 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=990.0,
    )

    clock = {"t": 1000.0}
    monkeypatch.setattr(vox_mod.time, "time", lambda: clock["t"])

    class _MidFlightTruthClient:
        def submit(self, *a, **kw):
            # Attempt started at `t = 1000.0`; at `t = 1005.0` another worker records HF's revealed `110.0s left`!
            def _result():
                hf_ledger.record_admission_refusal(sp, tok_race, 137.0, 110.0, update_space_gate=False, now_ts=1005.0)
                clock["t"] = 1020.0
                return (str(gen_wav), 25.0)

            return SimpleNamespace(done=lambda: True, status=lambda: None, result=_result)

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, _sp, hf_token=None: _MidFlightTruthClient()),
    )

    eng = vox_mod.VoxCPMEngine(work_dir=tmp_path / "w_race", spaces=[sp], hf_tokens=[tok_race])
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.worker"):
        out = worker_mod._synthesize_voxcpm_voice_job(
            eng,
            job_id="job_causality_1",
            text="မင်္ဂလာပါ ခင်ဗျာ။",
            output_wav=tmp_path / "race_out.wav",
            reference_wav=None,
            control_instruction="Narrator",
            preset_voice="komin",
            speed="+0%",
            progress_cb=None,
            tier="free",
        )
    assert out.is_file()
    # Debit of 26.0s was SKIPPED because truth_ts (1005.0) >= attempt_started_ts (1000.0): stays 110.0s!
    assert hf_ledger.get_token_remaining_s(tok_race, now_ts=1020.0) == pytest.approx(110.0)
    assert vox_mod.VoxCPMEngine._token_balances[tok_race] == pytest.approx(110.0)
    assert eng.truth_skip_debits == 1

    summaries = [r.getMessage() for r in caplog.records if "clone_prosody_summary" in r.getMessage()]
    assert len(summaries) == 1
    assert "truth_skip_debits=1" in summaries[0]
    assert "refusal_drift_deltas_s=[]" in summaries[0]

    # Also verify `record_failure_debit` honors the exact same causality skip when `attempt_started_ts <= truth_ts`
    skipped_fail: list[bool] = []
    hf_ledger.record_failure_debit(
        sp,
        tok_race,
        20.0,
        attempt_started_ts=1005.0,  # `truth_ts >= attempt_started_ts` (1005.0 >= 1005.0) -> skipped!
        skipped_out=skipped_fail,
        now_ts=1025.0,
    )
    assert skipped_fail == [True]
    assert hf_ledger.get_token_remaining_s(tok_race, now_ts=1025.0) == pytest.approx(110.0)

    # An attempt starting AFTER `truth_ts` (`1030.0 > 1005.0`) debits normally!
    skipped_after: list[bool] = []
    hf_ledger.record_failure_debit(
        sp,
        tok_race,
        15.0,
        attempt_started_ts=1030.0,
        skipped_out=skipped_after,
        now_ts=1040.0,
    )
    assert skipped_after == []
    assert hf_ledger.get_token_remaining_s(tok_race, now_ts=1040.0) == pytest.approx(95.0)
