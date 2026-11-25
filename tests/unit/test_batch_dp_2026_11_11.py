"""Batch DP (2026-11-11): paid-tier VoxCPM protection.

Item 1 — a paid-only RESERVE slice of the HF token pool: operator-
designated tokens (HF_RESERVE_TOKENS env var, owner amendment) ∪
the positional last slice ∪ learned-rich tokens (>= 900 s). Free
walks never attempt reserve tokens; paid walks use the full pool
(Batch DR, 2026-11-12, reversed DP's shared-first order: paid
walks now lead with the reserve inside each health class — see
test_batch_dr_2026_11_12.py).
Item 3 — park-and-ask: a paid job hitting VoxCPM quota exhaustion
PARKS (WAITING_CHOICE, charge held, no refund), the owner is asked
via bot prompt (wait / Edge-all / Edge-mix for recap; wait /
Edge-all for clone), a periodic resumer resumes on choice/capacity,
and a 48 h safety valve fails through the existing paths.
Item 4 — paid clone runs allow_fallback=False end-to-end; quota
exhaustion parks instead of the resilient fallback.
Item 5 — free jobs behave exactly as before (never park).

All clients/bots are fakes — no network, synthetic tokens only.
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

import app.services.recap_engine.core as core
from app.models.enums import AccountCategory, JobStatus
from app.models.schema import Job, User
from app.services import voice_choice as vc
from app.services import voxcpm_engine as vx
from app.services.ledger import get_minute_balance, get_or_create_user
from app.services.voxcpm_engine import VoxCPMEngine, VoxCPMQuotaExhaustedError

REPO = Path(__file__).resolve().parents[2]


# ── Cache token ─────────────────────────────────────────────────────

def test_dp_cache_token_is_r140():
    html = (REPO / "app/webapp/static/index.html").read_text(encoding="utf-8")
    assert html.count("20261125_admin_r1") == 5
    assert "r137" not in html


# ── Item 1: reserve composition ─────────────────────────────────────

@pytest.fixture(autouse=True)
def _dp_engine_state(monkeypatch):
    monkeypatch.setattr(VoxCPMEngine, "_token_balances", {})
    monkeypatch.setattr(VoxCPMEngine, "_token_cooldowns", {})
    monkeypatch.setattr(VoxCPMEngine, "_last_admission_price", None)
    monkeypatch.delenv("HF_RESERVE_TOKENS", raising=False)
    yield


def _pool(n, prefix="hf_t"):
    return [f"{prefix}{i}" for i in range(n)]


# ── Item 1: HF reserve retired in Round 15 Part F (single shared pool) ──
# Round 15 Part F retired the paid-only reserve slice and merged
# HF_RESERVE_TOKENS into get_available_hf_tokens() so every tier shares
# the full HF token pool under the same health/cooldown/rotation rules.

def test_dp_reserve_positional_31_pool():
    assert not hasattr(VoxCPMEngine, "_reserve_token_set")
    assert not hasattr(VoxCPMEngine, "_designated_reserve_tokens")


def test_dp_reserve_positional_edges(monkeypatch):
    monkeypatch.setenv("HF_TOKENS", ",".join(_pool(8)))
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HF_RESERVE_TOKENS", raising=False)
    assert vx.get_available_hf_tokens() == _pool(8)


def test_dp_reserve_learned_rich_anywhere():
    toks = _pool(5)
    VoxCPMEngine._token_balances = {toks[0]: 900.0, toks[1]: 899.99}
    assert not hasattr(VoxCPMEngine, "_RESERVE_RICH_BALANCE")


def test_dp_reserve_none_never_reserve(monkeypatch):
    monkeypatch.setenv("HF_TOKENS", ",".join(_pool(3)))
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HF_RESERVE_TOKENS", raising=False)
    assert None not in vx.get_available_hf_tokens()


def test_dp_reserve_designated_position_zero(monkeypatch):
    # Round 15 Part F: HF_RESERVE_TOKENS merges into the single shared pool.
    monkeypatch.setenv("HF_TOKENS", "hf_a,hf_b,hf_c,hf_d")
    monkeypatch.setenv("HF_RESERVE_TOKENS", "hf_pro")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    pool = vx.get_available_hf_tokens()
    assert "hf_pro" in pool
    assert {"hf_a", "hf_b", "hf_c", "hf_d", "hf_pro"} <= set(pool)


def test_dp_reserve_designated_parsing_tolerates_separators(monkeypatch):
    monkeypatch.delenv("HF_TOKENS", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setenv("HF_RESERVE_TOKENS", " hf_a,hf_b;hf_c  hf_d\n'hf_e' ,, ; ")
    assert set(vx.get_available_hf_tokens()) == {
        "hf_a", "hf_b", "hf_c", "hf_d", "hf_e",
    }
    monkeypatch.setenv("HF_RESERVE_TOKENS", "")
    assert vx.get_available_hf_tokens() == []
    monkeypatch.delenv("HF_RESERVE_TOKENS")
    assert vx.get_available_hf_tokens() == []


def test_dp_reserve_designated_not_in_pool_ignored(monkeypatch):
    # Round 15 Part F: tokens listed only in HF_RESERVE_TOKENS are INCLUDED
    # in the unified pool rather than gated/ignored.
    monkeypatch.setenv("HF_TOKENS", ",".join(_pool(4)))
    monkeypatch.setenv("HF_RESERVE_TOKENS", "hf_pro_only")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert "hf_pro_only" in vx.get_available_hf_tokens()


# ── Item 1: walk behaviour (DI-style fake clients) ──────────────────

def _write_wav(path: Path, seconds: float = 0.3, rate: int = 24000) -> Path:
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


class _GateClient:
    """Per-(space, token) behaviour: "ok" serves, "gate:<left>"
    refuses at the admission gate with the production quota text."""

    def __init__(self, behaviour, result_path, record, key):
        self.behaviour = behaviour
        self._result_path = result_path
        self.record = record
        self.key = key

    def submit(self, **kwargs):
        self.record.append(self.key)
        b = self.behaviour
        if isinstance(b, str) and b.startswith("gate:"):
            left = b.split(":", 1)[1]
            raise RuntimeError(
                "AppError: You have exceeded your free ZeroGPU quota "
                f"(94s requested vs. {left}s left)"
            )
        return _FakeJob(self._result_path)


def _engine(tmp_path, monkeypatch, tokens, spaces=("spaceA",)):
    monkeypatch.setattr(vx, "get_available_hf_tokens", lambda: list(tokens))
    monkeypatch.setattr(VoxCPMEngine, "_token_rr_counter", 0)
    monkeypatch.setattr(VoxCPMEngine, "_token_bind_rr_counters", {})
    monkeypatch.setattr(VoxCPMEngine, "_space_admission_prices", {})
    engine = VoxCPMEngine(work_dir=tmp_path / "cache")
    engine.spaces = list(spaces)
    engine.hf_tokens = list(tokens)
    return engine


def _patch_clients(monkeypatch, behaviours, result_path, record):
    clients = {
        key: _GateClient(behaviour, result_path, record, key)
        for key, behaviour in behaviours.items()
    }

    def _get_client(cls, space_name, hf_token=None):
        return clients[(space_name, hf_token)]

    monkeypatch.setattr(VoxCPMEngine, "get_client", classmethod(_get_client))


def _behaviours(tokens, default="ok", **overrides):
    out = {("spaceA", tok): default for tok in tokens}
    out[("spaceA", None)] = default
    for tok, behaviour in overrides.items():
        out[("spaceA", tok)] = behaviour
    return out


def test_dp_free_walk_never_attempts_reserve(tmp_path, monkeypatch):
    # Round 15 Part F: reserve retired — free walk succeeds on the single shared pool.
    toks = _pool(8)
    engine = _engine(tmp_path, monkeypatch, toks)
    record: list = []
    _patch_clients(monkeypatch, _behaviours(toks), _write_wav(tmp_path / "ok.wav"), record)
    out = engine.synthesize(
        text="မင်္ဂလာပါ", output_wav=tmp_path / "o.wav",
        allow_fallback=False, tier="free",
    )
    assert out.is_file()
    assert len(record) == 1


def test_dp_free_walk_reserve_excluded_even_when_shared_dead(tmp_path, monkeypatch):
    # Round 15 Part F: with the paid-only reserve retired, free tier CAN reach
    # healthy tokens anywhere in the unified pool (including the former tail slice)
    # when the first tokens are cooling down.
    toks = _pool(8)
    engine = _engine(tmp_path, monkeypatch, toks)
    now = time.time()
    monkeypatch.setattr(
        VoxCPMEngine, "_token_cooldowns",
        {tok: now + 1800 for tok in toks[:6]},
    )
    record: list = []
    behaviours = _behaviours(toks, default="gate:5", hf_t6="ok", hf_t7="ok")
    _patch_clients(monkeypatch, behaviours, _write_wav(tmp_path / "ok.wav"), record)
    out = engine.synthesize(
        text="မင်္ဂလာပါ", output_wav=tmp_path / "o.wav",
        allow_fallback=False, tier="free",
    )
    assert out.is_file()
    attempted = {tok for _sp, tok in record}
    assert "hf_t6" in attempted or "hf_t7" in attempted


def test_dp_free_walk_learned_rich_excluded(tmp_path, monkeypatch):
    # Round 15 Part F: learned-rich tokens are no longer gated off from free tier.
    toks = _pool(8)
    VoxCPMEngine._token_balances = {"hf_t0": 2400.0}
    engine = _engine(tmp_path, monkeypatch, toks)
    record: list = []
    _patch_clients(monkeypatch, _behaviours(toks), _write_wav(tmp_path / "ok.wav"), record)
    out = engine.synthesize(
        text="မင်္ဂလာပါ", output_wav=tmp_path / "o.wav",
        allow_fallback=False, tier="free",
    )
    assert out.is_file()
    assert record[0] == ("spaceA", "hf_t0")


def test_dp_free_fully_blocked_falls_back_when_allowed(tmp_path, monkeypatch):
    toks = _pool(8)
    engine = _engine(tmp_path, monkeypatch, toks)
    record: list = []
    _patch_clients(
        monkeypatch, _behaviours(toks, default="gate:5"),
        _write_wav(tmp_path / "ok.wav"), record,
    )
    fallback_wav = _write_wav(tmp_path / "persona.wav")

    def _fake_persona(text, persona_id, output_wav, speed=None, log_fn=None):
        Path(output_wav).write_bytes(fallback_wav.read_bytes())

    monkeypatch.setattr(
        "app.services.curated_voices.synthesize_curated_persona", _fake_persona
    )
    out = engine.synthesize(
        text="မင်္ဂလာပါ", output_wav=tmp_path / "o.wav",
        allow_fallback=True, tier="free", preset_voice="komin",
    )
    assert out.is_file()
    assert engine.last_fallback_used is True  # today's free behaviour


def test_dp_paid_walk_reserve_first_since_dr(tmp_path, monkeypatch):
    # Round 15 Part F retired paid-only reserve sorting; paid and free both walk
    # the single shared pool in uniform health + round-robin order.
    toks = _pool(8)
    engine = _engine(tmp_path, monkeypatch, toks)
    record: list = []
    _patch_clients(monkeypatch, _behaviours(toks), _write_wav(tmp_path / "ok.wav"), record)
    out = engine.synthesize(
        text="မင်္ဂလာပါ", output_wav=tmp_path / "o.wav",
        allow_fallback=False, tier="paid",
    )
    assert out.is_file()
    assert record[0][1] == "hf_t0"


def test_dp_paid_walk_can_use_reserve(tmp_path, monkeypatch):
    # Healthy tokens anywhere in the unified pool still serve paid when cooling
    # tokens sit in health class 2.
    toks = _pool(8)
    engine = _engine(tmp_path, monkeypatch, toks)
    now = time.time()
    monkeypatch.setattr(
        VoxCPMEngine, "_token_cooldowns",
        {tok: now + 1800 for tok in toks[:6]},
    )
    record: list = []
    behaviours = _behaviours(toks, default="gate:5", hf_t6="ok", hf_t7="ok")
    behaviours[("spaceA", None)] = "gate:5"
    _patch_clients(monkeypatch, behaviours, _write_wav(tmp_path / "ok.wav"), record)
    out = engine.synthesize(
        text="မင်္ဂလာပါ", output_wav=tmp_path / "o.wav",
        allow_fallback=False, tier="paid",
    )
    assert out.is_file()
    assert record[0][1] in {"hf_t6", "hf_t7"}


def test_dp_default_tier_is_free(tmp_path, monkeypatch):
    # Unplumbed caller (no tier kwarg) walks the unified pool cleanly.
    toks = _pool(8)
    engine = _engine(tmp_path, monkeypatch, toks)
    record: list = []
    _patch_clients(monkeypatch, _behaviours(toks), _write_wav(tmp_path / "ok.wav"), record)
    out = engine.synthesize(text="မင်္ဂလာပါ", output_wav=tmp_path / "o.wav", allow_fallback=False)
    assert out.is_file()
    assert record[0][1] == "hf_t0"


# ── Item 3: choice state, prompt, i18n ──────────────────────────────

def test_dp_choice_state_roundtrip_and_modes():
    state = vc.new_choice_state("recap")
    assert state["choice"] is None and state["prompt_message_id"] is None
    parsed = vc.parse_choice_state(vc.dump_choice_state(state))
    assert parsed["kind"] == "recap"
    assert vc.choice_mode(parsed) is None
    parsed["choice"] = "wait"
    assert vc.choice_mode(parsed) is None  # wait resumes the clone path
    parsed["choice"] = "edge_all"
    assert vc.choice_mode(parsed) == "edge_all"
    # SUPERSEDED by HF Pool Phase 1 Part 2 (#2, 2026-11-24): retired `edge_mix`
    # degrades cleanly to the Wait default (`None`).
    parsed["choice"] = "edge_mix"
    assert vc.choice_mode(parsed) is None
    assert vc.parse_choice_state(None) is None
    assert vc.parse_choice_state("not json") is None
    assert vc.parse_choice_state('{"no_kind": 1}') is None


def test_dp_keyboards_shapes():
    # SUPERSEDED by HF Pool Phase 1 Part 3 (#7, 2026-11-24): `dp:edgemix` retired,
    # `dp:cancel` added to both kinds, and `dp:edgeall` is Recap-only.
    kb = vc.build_choice_keyboard("rc_123", "recap", "my")
    datas = [row[0].callback_data for row in kb.inline_keyboard]
    assert datas == ["dp:wait:rc_123", "dp:edgeall:rc_123", "dp:cancel:rc_123"]
    kb2 = vc.build_choice_keyboard("job-uuid-36-chars-0000000000ab", "clone", "en")
    datas2 = [row[0].callback_data for row in kb2.inline_keyboard]
    assert datas2 == [
        "dp:wait:job-uuid-36-chars-0000000000ab",
        "dp:cancel:job-uuid-36-chars-0000000000ab",
    ]
    assert all(len(d.encode("utf-8")) <= 64 for d in datas + datas2)


def test_dp_prompt_is_estimate_in_both_locales():
    label = vc.quota_reset_estimate_label()
    assert len(label) == 5 and label[2] == ":"
    my_text = vc.choice_prompt_text("recap", "my")
    en_text = vc.choice_prompt_text("clone", "en")
    assert label in my_text and "ခန့်မှန်း" in my_text
    assert label in en_text and "Estimated" in en_text


def test_dp_i18n_keys_both_locales():
    # SUPERSEDED by HF Pool Phase 1 Part 3 (#8, 2026-11-24): owner-mandated copy
    # uses `{done}`, `{total}`, `{eta}` and replaces `dp_btn_edge_mix` with `dp_btn_cancel`.
    my = json.loads((REPO / "locales/my/messages.json").read_text(encoding="utf-8"))
    en = json.loads((REPO / "locales/en/messages.json").read_text(encoding="utf-8"))
    dp_keys = {k for k in my if k.startswith("dp_")}
    assert dp_keys, "no dp_ keys in my locale"
    assert dp_keys == {k for k in en if k.startswith("dp_")}
    for key in dp_keys:
        assert my[key].strip() and en[key].strip()
    assert "{eta}" in my["dp_park_prompt_recap"]
    assert "{eta}" in en["dp_park_prompt_clone"]
    assert my["dp_btn_wait"] == "⏳ Clone အသံနဲ့ပဲ စောင့်မယ်"
    assert my["dp_btn_edge_all"] == "🎙️ Gemini / Studio အသံနဲ့ အစက ပြန်လုပ်မယ် (Clone မိနစ် ပြန်အမ်းမယ်)"
    assert my["dp_btn_cancel"] == "❌ ရပ်ပြီး အပြည့် ပြန်အမ်းမယ်"
    assert "dp_btn_edge_mix" not in my and "dp_btn_edge_mix" not in en


def test_dp_miniapp_status_label():
    js_i18n = (REPO / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
    assert js_i18n.count("live_job_status_waiting_choice") == 2  # my + en
    app_js = (REPO / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
    assert '"WAITING_CHOICE"' in app_js


def test_dp_waiting_choice_status_model():
    assert JobStatus.WAITING_CHOICE.value == "WAITING_CHOICE"
    constraint = next(
        c for c in Job.__table__.constraints if c.name == "chk_job_status"
    )
    assert "WAITING_CHOICE" in str(constraint.sqltext)


# ── DB helpers ──────────────────────────────────────────────────────

@pytest.fixture
def _session_factory(db_session):
    @asynccontextmanager
    async def _factory():
        yield db_session

    return _factory


async def _mk_user(session, tid, category="FREE"):
    user, _ = await get_or_create_user(session, telegram_id=tid)
    user.account_category = category
    await session.commit()
    return user


async def _mk_cloud_model(session):
    from app.models.schema import VoiceModel

    model = VoiceModel(
        name="🎙️ VoxCPM (Cloud AI)",
        slug="voxcpm-cloud-ai",
        type="SYSTEM",
        status="ACTIVE",
        allowed_for_free=True,
        allowed_for_purchased=True,
        is_default_free=True,
        pth_path="voxcpm",
        model_dir="cloud",
    )
    session.add(model)
    await session.commit()
    return model


def _state(kind, choice=None, parked_at=None, prompt=True):
    state = vc.new_choice_state(kind)
    state["choice"] = choice
    if parked_at is not None:
        state["parked_at"] = parked_at
    if prompt:
        state["prompt_chat_id"] = 4242
        state["prompt_message_id"] = 99
    return vc.dump_choice_state(state)


async def _mk_job(session, user_id, *, kind="clone", status="WAITING_CHOICE",
                  state=None, job_id=None):
    job = Job(
        id=job_id or str(uuid.uuid4()),
        user_id=user_id,
        job_type="RECAP" if kind == "recap" else "VOICE",
        engine="recap" if kind == "recap" else "voxcpm",
        status=status,
        input_text="စမ်းသပ် စာသား",
        chat_id=user_id,
        voice_choice_state=state,
    )
    session.add(job)
    await session.commit()
    return job


async def _fresh_job(session, job_id):
    session.expunge_all()
    return await session.get(Job, job_id)


# ── Item 3: callbacks ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dp_apply_wait_records_and_stays_parked(db_session):
    user = await _mk_user(db_session, 61001)
    job = await _mk_job(db_session, user.id, state=_state("clone"))
    outcome = await vc.apply_user_choice(db_session, job, "wait", user.id)
    assert outcome == "ok_wait"
    fresh = await _fresh_job(db_session, job.id)
    assert fresh.status == "WAITING_CHOICE"
    assert vc.parse_choice_state(fresh.voice_choice_state)["choice"] == "wait"


@pytest.mark.asyncio
async def test_dp_apply_non_owner_refused(db_session):
    owner = await _mk_user(db_session, 61002)
    other = await _mk_user(db_session, 61003)
    job = await _mk_job(db_session, owner.id, state=_state("recap"))
    outcome = await vc.apply_user_choice(db_session, job, "edge_all", other.id)
    assert outcome == "not_owner"
    fresh = await _fresh_job(db_session, job.id)
    assert fresh.status == "WAITING_CHOICE"
    assert vc.parse_choice_state(fresh.voice_choice_state)["choice"] is None


@pytest.mark.asyncio
async def test_dp_apply_edge_all_requeues(db_session):
    user = await _mk_user(db_session, 61004)
    job = await _mk_job(db_session, user.id, state=_state("recap"))
    outcome = await vc.apply_user_choice(db_session, job, "edge_all", user.id)
    assert outcome == "ok_resume"
    fresh = await _fresh_job(db_session, job.id)
    assert fresh.status == "QUEUED"
    assert vc.parse_choice_state(fresh.voice_choice_state)["choice"] == "edge_all"


@pytest.mark.asyncio
async def test_dp_apply_edge_mix_rejected_for_clone(db_session):
    user = await _mk_user(db_session, 61005)
    job = await _mk_job(db_session, user.id, state=_state("clone"))
    outcome = await vc.apply_user_choice(db_session, job, "edge_mix", user.id)
    assert outcome == "bad_choice"
    fresh = await _fresh_job(db_session, job.id)
    assert fresh.status == "WAITING_CHOICE"


@pytest.mark.asyncio
async def test_dp_apply_stale_job_is_polite_noop(db_session):
    user = await _mk_user(db_session, 61006)
    job = await _mk_job(db_session, user.id, status="RUNNING", state=None)
    outcome = await vc.apply_user_choice(db_session, job, "wait", user.id)
    assert outcome == "not_parked"


@pytest.mark.asyncio
async def test_dp_callback_handler_owner_flow(db_session, _session_factory, monkeypatch):
    import app.handlers.clone as clone_handlers
    from app.services.i18n import t

    monkeypatch.setattr(clone_handlers, "AsyncSessionLocal", _session_factory)
    owner = await _mk_user(db_session, 61007)
    stranger = await _mk_user(db_session, 61008)
    job = await _mk_job(db_session, owner.id, state=_state("recap"))

    async def _tap(actor_id, action):
        answers = []
        bot = SimpleNamespace(edit_message_reply_markup=AsyncMock())

        async def _answer(text=None, **kw):
            answers.append(text)

        callback = SimpleNamespace(
            data=f"dp:{action}:{job.id}",
            from_user=SimpleNamespace(id=actor_id),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=owner.id), message_id=99
            ),
            bot=bot,
            answer=_answer,
        )
        await clone_handlers.handle_voice_choice_callback(callback)
        return answers, bot

    # A stranger's tap changes nothing and gets the owner-only answer.
    answers, bot = await _tap(stranger.id, "edgeall")
    assert answers == [t("dp_choice_not_owner", "my")]
    assert (await _fresh_job(db_session, job.id)).status == "WAITING_CHOICE"
    bot.edit_message_reply_markup.assert_not_awaited()

    # The owner's Edge tap re-queues and retires the buttons.
    answers, bot = await _tap(owner.id, "edgeall")
    assert answers == [t("dp_choice_resume_answer", "my")]
    assert (await _fresh_job(db_session, job.id)).status == "QUEUED"
    bot.edit_message_reply_markup.assert_awaited_once()


# ── Item 3: resumer sweep ───────────────────────────────────────────

@pytest.fixture
def _sweep_env(db_session, _session_factory, monkeypatch):
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", _session_factory)
    return db_session


@pytest.mark.asyncio
async def test_dp_sweep_resumes_edge_choice(_sweep_env):
    # SUPERSEDED by HF Pool Phase 1 Part 3 (#7, 2026-11-24): `edge_all` is Recap-only.
    user = await _mk_user(_sweep_env, 62001)
    job = await _mk_job(
        _sweep_env, user.id, kind="recap", job_id="rc_dp_edge_1",
        state=_state("recap", choice="edge_all"),
    )
    counts = await vc.sweep_parked_jobs()
    assert counts["resumed"] == 1
    assert (await _fresh_job(_sweep_env, job.id)).status == "QUEUED"


@pytest.mark.asyncio
async def test_dp_sweep_wait_resumes_only_with_capacity(_sweep_env, monkeypatch):
    user = await _mk_user(_sweep_env, 62002)
    job = await _mk_job(_sweep_env, user.id, state=_state("recap", choice="wait"))
    monkeypatch.setattr(vc, "voxcpm_capacity_available", lambda: False)
    counts = await vc.sweep_parked_jobs()
    assert counts["resumed"] == 0 and counts["still_parked"] == 1
    assert (await _fresh_job(_sweep_env, job.id)).status == "WAITING_CHOICE"
    monkeypatch.setattr(vc, "voxcpm_capacity_available", lambda: True)
    counts = await vc.sweep_parked_jobs()
    assert counts["resumed"] == 1
    assert (await _fresh_job(_sweep_env, job.id)).status == "QUEUED"


@pytest.mark.asyncio
async def test_dp_sweep_expires_clone_after_48h(_sweep_env, monkeypatch):
    user = await _mk_user(_sweep_env, 62003)
    old = time.time() - 49 * 3600
    job = await _mk_job(_sweep_env, user.id, state=_state("clone", parked_at=old))
    monkeypatch.setattr(vc, "voxcpm_capacity_available", lambda: False)
    counts = await vc.sweep_parked_jobs()
    assert counts["expired"] == 1
    fresh = await _fresh_job(_sweep_env, job.id)
    assert fresh.status == "FAILED"
    assert fresh.error_message  # curated expiry reason stored


@pytest.mark.asyncio
async def test_dp_sweep_expires_recap_via_existing_failure_path(_sweep_env, monkeypatch):
    import app.webapp.api as api

    user = await _mk_user(_sweep_env, 62004, category="PURCHASED")
    job = await _mk_job(
        _sweep_env, user.id, kind="recap", job_id="rc_dp_expire_1",
        state=_state("recap", parked_at=time.time() - 49 * 3600),
    )
    job.recap_config = json.dumps({
        "config": {"url": "movie.mp4", "title": "T", "voice_slug": "komin"},
        "is_vip": True, "phase": "full",
        "credit_deducted": True, "credits_charged": 1,
        "credit_refunded": False, "credits_refunded": 0,
    })
    await _sweep_env.commit()
    refund = AsyncMock(return_value=True)
    dm = AsyncMock(return_value=True)
    monkeypatch.setattr(api, "_refund_recap_job_charge", refund)
    monkeypatch.setattr(api, "_send_recap_failure_dm", dm)
    monkeypatch.setattr(vc, "voxcpm_capacity_available", lambda: False)
    counts = await vc.sweep_parked_jobs()
    assert counts["expired"] == 1
    fresh = await _fresh_job(_sweep_env, job.id)
    assert fresh.status == "FAILED"
    refund.assert_awaited_once()  # the EXISTING refund path fired
    dm.assert_awaited_once()


def test_dp_capacity_probe_signals(monkeypatch):
    # SUPERSEDED by HF Pool Phase 1 Part 3 (#4, 2026-11-24): resumer reads the shared
    # `aj_hf:pool_state` ledger and requires `effective_usable_gpu_s >= needed * 1.25`.
    from app.services import hf_pool_ledger as hfl

    now = time.time()
    d_a = hfl.token_digest("hf_a")
    d_b = hfl.token_digest("hf_b")
    # Unseeded / all cooling -> no capacity.
    hfl.save_pool_state({
        "tokens": {
            d_a: {"status": "ok", "remaining_s": 0.0, "base_s": 300.0, "resets_at": "", "cooldown_until": str(now + 600), "updated_at": ""},
            d_b: {"status": "ok", "remaining_s": 0.0, "base_s": 300.0, "resets_at": "", "cooldown_until": str(now + 600), "updated_at": ""},
        },
        "space_gates": {"spaceA": 94},
        "in_flight_gpu_s": 0,
    }, now_ts=now)
    assert vc.voxcpm_capacity_available(94.0) is False
    # Healthy but below 94 * 1.25 = 117.5s -> no.
    hfl.save_pool_state({
        "tokens": {
            d_b: {"status": "ok", "remaining_s": 100.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
        },
        "space_gates": {"spaceA": 94},
        "in_flight_gpu_s": 0,
    }, now_ts=now)
    assert vc.voxcpm_capacity_available(94.0) is False
    # Healthy with >= 117.5s usable -> yes.
    hfl.save_pool_state({
        "tokens": {
            d_b: {"status": "ok", "remaining_s": 300.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
        },
        "space_gates": {"spaceA": 94},
        "in_flight_gpu_s": 0,
    }, now_ts=now)
    assert vc.voxcpm_capacity_available(94.0) is True


# ── Item 3/5: recap core park + resume modes ────────────────────────

class _VoxFake:
    def __init__(self, exc):
        self.exc = exc
        self.calls: list = []

    def synthesize(self, **kwargs):
        self.calls.append(kwargs)
        raise self.exc


class _EdgeFake:
    def __init__(self, wav_path):
        self.wav = wav_path
        self.calls = 0

    def generate_channel99_voice(self, text, voice):
        self.calls += 1
        return self.wav


def _runner(paid=False, mode=None):
    runner = core.EdgeSmartSync.__new__(core.EdgeSmartSync)
    runner.log = lambda msg: None
    runner.progress = lambda a, b, c: None
    runner.overlays = {}
    runner.voice = "voxcpm_testvoice"
    runner.paid_tier = paid
    runner.voice_choice_mode = mode
    runner.ffprobe = shutil.which("ffprobe") or "/usr/bin/ffprobe"
    runner.max_video_speed = 1.25
    return runner


def _seg(idx=1, start=0.0, end=3.0):
    return {
        "segment_id": idx,
        "video_start": start,
        "video_end": end,
        "narration": "ဇာတ်လမ်း အပိုဒ် တစ်ပိုဒ် ပြောပြနေပါတယ်",
    }


def _produce(runner, seg, tmp_path, checkpoint, vox, edge):
    parts = tmp_path / "parts"
    parts.mkdir(parents=True, exist_ok=True)
    return runner._produce_segment_audio(
        seg["segment_id"], seg,
        paths=SimpleNamespace(tts_parts=parts),
        checkpoint=checkpoint,
        fingerprint_voice="voxcpm_komin",
        engine_provider="voxcpm",
        use_gemini=False, gem_engine=None, gemini_voice_name="",
        use_voxcpm=True, vox_engine=vox,
        persona_ref_wav=None, persona_ref_text="",
        _global_preset_v=None, voxcpm_speed_float=1.0,
        effective_rate="+0%", effective_pitch="+0Hz",
        base_edge_voice="my-MM-ThihaNeural",
        engine=edge, short_pauses=False, total_segments=2,
    )


def test_dp_core_paid_quota_parks(tmp_path):
    vox = _VoxFake(VoxCPMQuotaExhaustedError("pool dead"))
    edge = _EdgeFake(_write_wav(tmp_path / "edge.wav", seconds=1.0))
    runner = _runner(paid=True)
    with pytest.raises(core.RecapParkRequested):
        _produce(runner, _seg(), tmp_path, {"audio": {}}, vox, edge)
    assert edge.calls == 0  # no silent persona degradation


def test_dp_core_free_quota_falls_back_per_segment(tmp_path):
    # SUPERSEDED by HF Pool Phase 1 Part 2 (#1 & #4, 2026-11-24): Free quota
    # exhaustion raises `RecapFreeQuotaExhaustedError` with ZERO Edge fallback calls.
    vox = _VoxFake(VoxCPMQuotaExhaustedError("pool dead"))
    edge = _EdgeFake(_write_wav(tmp_path / "edge.wav", seconds=1.0))
    runner = _runner(paid=False)
    with pytest.raises(core.RecapFreeQuotaExhaustedError):
        _produce(runner, _seg(), tmp_path, {"audio": {}}, vox, edge)
    assert edge.calls == 0


def test_dp_core_paid_nonquota_falls_back(tmp_path):
    # SUPERSEDED by HF Pool Phase 1 Part 2 (#1 & #4, 2026-11-24): deterministic
    # non-quota error raises `RecapCloneSynthesisError` with ZERO Edge fallback calls.
    vox = _VoxFake(RuntimeError("space exploded"))
    edge = _EdgeFake(_write_wav(tmp_path / "edge.wav", seconds=1.0))
    runner = _runner(paid=True)
    with pytest.raises(core.RecapCloneSynthesisError):
        _produce(runner, _seg(), tmp_path, {"audio": {}}, vox, edge)
    assert edge.calls == 0


def test_dp_core_edge_mix_keeps_produced_persona_rest(tmp_path):
    # SUPERSEDED by HF Pool Phase 1 Part 2 (#2, 2026-11-24): `edge_mix` is retired;
    # legacy parked state with `choice="edge_mix"` degrades to `None` (Wait).
    state = vc.new_choice_state("recap")
    state["choice"] = "edge_mix"
    assert vc.choice_mode(state) is None


def test_dp_core_edge_all_redoes_produced(tmp_path):
    seg1 = _seg(1, 0.0, 3.0)
    parts = tmp_path / "parts"
    parts.mkdir(parents=True, exist_ok=True)
    dst1 = parts / f"{core.segment_basename(seg1)}.wav"
    _write_wav(dst1, seconds=1.0)
    checkpoint = {"audio": {
        core.segment_basename(seg1): {
            "fingerprint": core.segment_fingerprint(seg1, "voxcpm_komin"),
        },
    }}
    vox = _VoxFake(AssertionError("VoxCPM must not be attempted in edge_all"))
    edge = _EdgeFake(_write_wav(tmp_path / "edge.wav", seconds=1.0))
    runner = _runner(paid=True, mode="edge_all")
    redone = _produce(runner, seg1, tmp_path, checkpoint, vox, edge)
    assert redone["record"] is not None  # re-produced, not reused
    assert redone["record"]["provider"] == "edge_fallback"
    assert edge.calls == 1 and vox.calls == []


def test_dp_core_checkpoint_mode_adoption_pin():
    src = Path(core.__file__).read_text(encoding="utf-8")
    assert 'checkpoint.get("voice_choice_mode")' in src
    assert 'checkpoint["voice_choice_mode"] = self.voice_choice_mode' in src


def test_dp_core_threads_tier_to_engine():
    src = Path(core.__file__).read_text(encoding="utf-8")
    assert 'tier="paid" if self.paid_tier else "free"' in src


# ── Item 3: recap driver parks (no refund, prompt once) ─────────────

class _FakeBot:
    def __init__(self):
        self.sent: list = []
        self.edited: list = []
        self.session = SimpleNamespace(close=AsyncMock())

    async def send_message(self, chat_id=None, text=None, reply_markup=None, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return SimpleNamespace(message_id=555)

    async def edit_message_text(self, **kw):
        self.edited.append(kw)
        return SimpleNamespace(message_id=kw.get("message_id"))

    async def edit_message_reply_markup(self, **kw):
        self.edited.append(kw)


@pytest.mark.asyncio
async def test_dp_recap_driver_parks_without_refund(
    db_session, _session_factory, monkeypatch, tmp_path
):
    import app.webapp.api as api
    from app.services.recap_engine.core import RecapParkRequested

    user = await _mk_user(db_session, 63001, category="PURCHASED")
    job_id = "rc_dp_park_1"
    job = Job(
        id=job_id, user_id=user.id, job_type="RECAP", engine="recap",
        status="RUNNING", input_text="T", chat_id=user.id,
    )
    db_session.add(job)
    await db_session.commit()

    src_file = tmp_path / "src.mp4"
    src_file.write_bytes(b"\x00" * 2048)
    entry = {
        "id": job_id, "user_id": user.id, "title": "T",
        "status": "QUEUED", "credit_deducted": True, "credits_charged": 1,
        "credit_refunded": False, "credits_refunded": 0,
    }
    api.RECAP_JOBS_REGISTRY[job_id] = entry

    class _StubDownloader:
        def generate_recap_video(self, **kwargs):
            raise RecapParkRequested("quota exhausted")

    fake_bot = _FakeBot()
    refund = AsyncMock(return_value=True)
    fail_dm = AsyncMock(return_value=True)
    monkeypatch.setattr(api, "AsyncSessionLocal", _session_factory)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", _session_factory)
    monkeypatch.setattr(
        "app.services.downloader.MediaDownloader", _StubDownloader
    )
    monkeypatch.setattr(api, "_refund_recap_job_charge", refund)
    monkeypatch.setattr(api, "_send_recap_failure_dm", fail_dm)
    monkeypatch.setattr("aiogram.Bot", lambda **kw: fake_bot)
    try:
        req = api.RecapConfigRequest(url=str(src_file), title="T", voice_slug="komin")
        await api._execute_recap_background_job(
            job_id, req, user.id, user.id,
            is_vip=True, credit_deducted=True, phase="full",
        )
    finally:
        api.RECAP_JOBS_REGISTRY.pop(job_id, None)

    fresh = await _fresh_job(db_session, job_id)
    assert fresh.status == "WAITING_CHOICE"          # parked, NOT failed
    state = vc.parse_choice_state(fresh.voice_choice_state)
    assert state["kind"] == "recap" and state["prompt_message_id"] == 555
    assert entry["status"] == "WAITING_CHOICE"
    assert len(fake_bot.sent) == 1                    # prompt sent once
    refund.assert_not_awaited()                       # charge stays held
    fail_dm.assert_not_awaited()


# ── Items 4/5: clone worker ─────────────────────────────────────────

class _WorkerBot(_FakeBot):
    async def send_audio(self, **kw):
        self.sent.append({"audio": True})


def _quota_raise(record):
    def _fake(text=None, output_wav=None, **kwargs):
        record.update(kwargs)
        raise VoxCPMQuotaExhaustedError("pool dead")
    return _fake


@pytest.mark.asyncio
async def test_dp_clone_paid_quota_parks(db_session, tmp_path):
    from app.config import settings
    from app.services.queue import create_voice_clone_job
    from app.worker import process_single_job

    user = await _mk_user(db_session, 64001, category="PURCHASED")
    await _mk_cloud_model(db_session)
    bal = await get_minute_balance(db_session, user.id)
    bal.purchased_seconds = 600
    await db_session.commit()

    ref_dir = settings.storage_root / "temp" / "inputs"
    ref_dir.mkdir(parents=True, exist_ok=True)
    ref_file = ref_dir / "dp_ref.wav"
    _write_wav(ref_file, seconds=1.0)
    job, status = await create_voice_clone_job(
        session=db_session, user_id=user.id,
        text="စမ်းသပ် စာသား တစ်ခု",
        source_audio_path=str(ref_file),
        chat_id=user.id,
    )
    assert status == "QUEUED"
    await db_session.commit()

    record: dict = {}
    bot = _WorkerBot()
    try:
        with patch(
            "app.services.voxcpm_engine.VoxCPMEngine.synthesize",
            side_effect=_quota_raise(record),
        ):
            result = await process_single_job(
                session=db_session, job=job, bot=bot
            )
    finally:
        ref_file.unlink(missing_ok=True)
    assert result is False
    assert job.status == "WAITING_CHOICE"
    state = vc.parse_choice_state(job.voice_choice_state)
    assert state["kind"] == "clone" and state["prompt_message_id"] == 555
    assert len(bot.sent) == 1  # the park prompt, once
    # Tier plumbing reached the engine call.
    assert record.get("tier") == "paid"
    assert record.get("allow_fallback") is False
    # No minutes consumed; the reference survived in the park store.
    bal_after = await get_minute_balance(db_session, user.id)
    assert bal_after.purchased_seconds == 600
    assert job.seconds_consumed in (0, None)
    parked_ref = Path(job.source_audio_path)
    assert "dp_refs" in str(parked_ref) and parked_ref.is_file()
    parked_ref.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_dp_clone_free_quota_uses_resilient_fallback(db_session):
    # SUPERSEDED by HF Pool Phase 1 Part 2 (#1 & #4, 2026-11-24): Free standalone
    # clone fails-fast on quota exhaustion (0 s deducted, no parking, ZERO Edge calls,
    # localized `worker_error_quota_exhausted`).
    from app.services.queue import create_voice_clone_job
    from app.worker import process_single_job

    user = await _mk_user(db_session, 64002)  # FREE
    await _mk_cloud_model(db_session)
    job, status = await create_voice_clone_job(
        session=db_session, user_id=user.id,
        text="စမ်းသပ် စာသား တစ်ခု", chat_id=user.id,
    )
    assert status == "QUEUED"
    await db_session.commit()

    record: dict = {}
    persona_calls: list = []

    def _fake_persona(text, persona_id, output_wav, speed=None, **kw):
        persona_calls.append(persona_id)
        _write_wav(Path(output_wav), seconds=1.0)

    bot = _WorkerBot()
    with (
        patch(
            "app.services.voxcpm_engine.VoxCPMEngine.synthesize",
            side_effect=_quota_raise(record),
        ),
        patch(
            "app.services.curated_voices.synthesize_curated_persona",
            side_effect=_fake_persona,
        ),
    ):
        result = await process_single_job(session=db_session, job=job, bot=bot)
    assert result is False
    assert job.status == "FAILED"
    assert persona_calls == []
    assert job.seconds_consumed in (0, None)
    assert job.voice_choice_state is None
    assert "မိနစ် လုံးဝ မဖြတ်ထားပါ" in (job.error_message or "")
    assert record.get("allow_fallback") is False


@pytest.mark.asyncio
async def test_dp_clone_paid_edge_all_resume_skips_primary(db_session):
    # SUPERSEDED by HF Pool Phase 1 Part 2 (#1) & Part 3 (#7, 2026-11-24):
    # `edge_all` is Recap-only (standalone clone rejects `edge_all` and supports
    # `wait` + `cancel` with 100% immediate refund).
    from app.services.queue import create_voice_clone_job

    user = await _mk_user(db_session, 64003, category="PURCHASED")
    await _mk_cloud_model(db_session)
    bal = await get_minute_balance(db_session, user.id)
    bal.purchased_seconds = 600
    await db_session.commit()
    job, status = await create_voice_clone_job(
        session=db_session, user_id=user.id,
        text="စမ်းသပ် စာသား တစ်ခု", chat_id=user.id,
    )
    assert status == "QUEUED"
    job.voice_choice_state = _state("clone", choice=None)
    job.status = "WAITING_CHOICE"
    await db_session.commit()

    assert await vc.apply_user_choice(db_session, job, "edge_all", user.id) == "bad_choice"
    assert await vc.apply_user_choice(db_session, job, "cancel", user.id) == "ok_cancel"
    fresh = await _fresh_job(db_session, job.id)
    assert fresh.status == "CANCELLED"
    bal_after = await get_minute_balance(db_session, user.id)
    assert bal_after.purchased_seconds == 600


# ── Status surface ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dp_waiting_choice_roundtrips_status_endpoint(db_session):
    import app.webapp.api as api

    user = await _mk_user(db_session, 65001)
    job = await _mk_job(db_session, user.id, state=_state("clone"))
    payload = await api.get_job_status(job.id, user=user, db=db_session)
    assert payload["status"] == "WAITING_CHOICE"


@pytest.mark.asyncio
async def test_dp_waiting_choice_not_counted_as_active_recap(db_session):
    import app.webapp.api as api

    user = await _mk_user(db_session, 65002)
    await _mk_job(
        db_session, user.id, kind="recap", job_id="rc_dp_park_count",
        state=_state("recap"),
    )
    assert await api._recap_active_job_count(db_session, user.id) == 0
