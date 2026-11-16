"""Batch CG (2026-11-07) — bring-your-own Gemini key.

Owner design (2026-11-07 14:02–14:27): Gemini TTS routes the user's
OWN key first, then the service pool, then Edge (Batch CE fallback).
FREE users get a pool-funded taste (free_gemini_taste_minutes of
delivered Gemini audio per day) bounded by a global FREE daily
request budget; the 15/60 TTS day caps still count every engine; a
user's key serves ONLY that user, is never logged or echoed (masked
•••• + last 4 only), validates with ONE minimal engine call before
storage, and never fails a job when it breaks (marked invalid, the
job continues on the pool step).

These tests pin:
  * the routing order per tier × key state (own → pool → Edge);
  * taste + global-budget enforcement, debited on SUCCESSFUL draws
    only (failed attempts consume nothing);
  * a mid-job auth-class key failure marks the key + tells the user
    + the job still succeeds on the pool;
  * the sentinel key value NEVER appears in any log line, API
    response or UI payload (negative pins);
  * caps interplay — an own-key job's minutes still count toward
    the 15/60 TTS day;
  * validate-on-save makes exactly one engine call and never stores
    an invalid key;
  * the recap path shares the routing (routed composite inside
    EdgeSmartSync + the pipeline/downloader pass-through).
"""

from __future__ import annotations

import inspect
import wave
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config.settings import settings
from app.models.enums import AccountCategory
from app.models.schema import GeminiPoolUsage, Job, User
from app.services import gemini_key_service as gks
from app.services import tts_service
from app.services.recap_engine import gemini_tts_engine as gte

ROOT = Path(__file__).resolve().parents[2]

SENTINEL_KEY = "AIzaSySENTINELcgTESTKEYvalue9876wxyz"
pytestmark_ffmpeg = pytest.mark.skipif(
    not __import__("shutil").which("ffmpeg"), reason="needs ffmpeg"
)


def _write_wav(path: Path, seconds: float = 1.0, rate: int = 24000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


class _FakeGeminiEngine:
    """One-key (or pool) GeminiTTSEngine double with a scripted fate."""

    instances: list["_FakeGeminiEngine"] = []

    def __init__(self, *, ffmpeg, work_dir, rate="+0%", keys=None, direction="", log=None,
                 record_usage=True):
        self.keys = list(keys or [])
        self.work_dir = Path(work_dir)
        self.exhausted = False
        self.behaviour = "ok"  # ok | exhaust | invalid | spent
        # Batch DN: own-key engines are built with record_usage=False;
        # the double records which constructions got the flag so the
        # CG tests can assert BYOK engines never write pool usage.
        self.record_usage = record_usage
        self.key_error_codes: dict[int, int] = {}
        self.key_error_texts: dict[int, str] = {}
        self.calls = 0
        type(self).instances.append(self)

    def generate_channel99_voice(self, text, voice):
        self.calls += 1
        if self.behaviour == "exhaust":
            self.exhausted = True
            self.key_error_codes = {i: 429 for i in range(len(self.keys))}
            self.key_error_texts = {
                i: "429 RESOURCE_EXHAUSTED: quota exhausted"
                for i in range(len(self.keys))
            }
            raise gte.GeminiTTSError(
                "Gemini TTS: all keys' quota is exhausted or unavailable "
                "for today — every configured key/model was rejected"
            )
        if self.behaviour == "invalid":
            self.key_error_codes = {0: 400}
            self.key_error_texts = {
                0: '400 API_KEY_INVALID: "API key not valid. '
                'Please pass a valid API key."'
            }
            raise gte.GeminiTTSError(
                "Gemini TTS: all keys' quota is exhausted or unavailable "
                "for today — every configured key/model was rejected "
                "(400 API_KEY_INVALID)"
            )
        if self.behaviour == "spent":
            self.key_error_codes = {0: 429}
            self.key_error_texts = {0: "429 RESOURCE_EXHAUSTED: quota"}
            raise gte.GeminiTTSError(
                "Gemini TTS: all keys' quota is exhausted or unavailable "
                "for today — every configured key/model was rejected (429)"
            )
        return _write_wav(
            self.work_dir / f"seg_{abs(hash((text, len(text)))) % 99999}.wav"
        )


class _FakeEdgeEngine:
    def __init__(self, *a, **kw):
        pass

    def generate_channel99_voice(self, text, voice):
        return _write_wav(
            Path(settings.storage_root) / f"edge_{abs(hash(text)) % 99999}.wav"
        )


@pytest.fixture
def engines(monkeypatch):
    from app.services.recap_engine import core as core_mod

    _FakeGeminiEngine.instances = []
    monkeypatch.setattr(gte, "GeminiTTSEngine", _FakeGeminiEngine)
    monkeypatch.setattr(core_mod, "collect_gemini_keys", lambda: ["POOLKEY1"], raising=False)
    monkeypatch.setattr(gte, "collect_gemini_keys", lambda: ["POOLKEY1"])
    # EdgeTTSEngine is imported into tts_service's function scope from
    # core — patch it at its home module (the CE fixture pattern).
    monkeypatch.setattr(core_mod, "EdgeTTSEngine", _FakeEdgeEngine)
    return _FakeGeminiEngine


def _ctx(
    *,
    tier="FREE",
    is_admin=False,
    own_key=None,
    state="",
    taste_limit=120.0,
    taste_used=0.0,
    budget_limit=100,
    budget_used=0,
    user_id=4242,
):
    return gks.GeminiRouteContext(
        user_id=user_id,
        tier=tier,
        is_admin=is_admin,
        own_key=own_key,
        own_key_state=state,
        taste_limit_seconds=taste_limit,
        taste_used_seconds=taste_used,
        budget_limit_requests=budget_limit,
        budget_used_requests=budget_used,
    )


def _job(text="မင်္ဂလာပါ။ နေကောင်းလား။"):
    return Job(
        id="cg_test_job",
        user_id=4242,
        chat_id=4242,
        job_type="tts",
        engine="gemini",
        preset_voice="Kore",
        input_text=text,
        status="RUNNING",
        speed_preset="+12%",
    )


def _synth(job, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    return tts_service._synthesize_tts_audio_sync(job)


# ── Routing order per tier × key state ───────────────────────────────

@pytestmark_ffmpeg
def test_cg_free_with_valid_key_routes_own_key_first(engines, monkeypatch, tmp_path):
    job = _job()
    job._cg_route_ctx = _ctx(own_key=SENTINEL_KEY, state="ok")
    _synth(job, monkeypatch, tmp_path)
    assert job.tts_route == "own_key"
    assert not job.tts_fallback_used
    # The FIRST engine built holds the user's key; the pool engine
    # was built too but never called.
    own, pool = engines.instances[0], engines.instances[1]
    assert own.keys == [SENTINEL_KEY]
    assert own.calls == 1 and pool.calls == 0
    assert job._cg_outcome["pool_requests"] == 0


@pytestmark_ffmpeg
def test_cg_free_no_key_within_taste_uses_pool(engines, monkeypatch, tmp_path):
    job = _job()
    job._cg_route_ctx = _ctx()
    _synth(job, monkeypatch, tmp_path)
    assert job.tts_route == "pool"
    assert job._cg_outcome["pool_requests"] == 1
    assert job._cg_outcome["pool_seconds"] == pytest.approx(1.0)


@pytestmark_ffmpeg
def test_cg_free_taste_spent_goes_edge_with_taste_route(engines, monkeypatch, tmp_path):
    job = _job()
    job._cg_route_ctx = _ctx(taste_used=120.0)
    _synth(job, monkeypatch, tmp_path)
    assert job.tts_route == "edge_taste"
    assert job.tts_fallback_used is True  # caption names Edge + note
    assert engines.instances == []  # no Gemini engine was even built


@pytestmark_ffmpeg
def test_cg_free_global_budget_spent_also_blocks_pool(engines, monkeypatch, tmp_path):
    job = _job()
    job._cg_route_ctx = _ctx(taste_used=0.0, budget_used=100)
    _synth(job, monkeypatch, tmp_path)
    assert job.tts_route == "edge_taste"


@pytestmark_ffmpeg
def test_cg_purchased_no_key_pool_not_taste_governed(engines, monkeypatch, tmp_path):
    job = _job()
    job._cg_route_ctx = _ctx(tier="PURCHASED", taste_used=99999.0, budget_used=99999)
    _synth(job, monkeypatch, tmp_path)
    assert job.tts_route == "pool"


@pytestmark_ffmpeg
def test_cg_key_marked_invalid_state_skips_key(engines, monkeypatch, tmp_path):
    job = _job()
    job._cg_route_ctx = _ctx(own_key=SENTINEL_KEY, state="invalid")
    _synth(job, monkeypatch, tmp_path)
    assert job.tts_route == "pool"
    assert all(inst.keys != [SENTINEL_KEY] for inst in engines.instances)


@pytestmark_ffmpeg
def test_cg_admin_exempt_from_taste(engines, monkeypatch, tmp_path):
    job = _job()
    job._cg_route_ctx = _ctx(is_admin=True, taste_used=99999.0)
    _synth(job, monkeypatch, tmp_path)
    assert job.tts_route == "pool"


# ── Key failure mid-job ──────────────────────────────────────────────

@pytestmark_ffmpeg
def test_cg_invalid_key_mid_job_marks_and_continues_on_pool(
    engines, monkeypatch, tmp_path
):
    job = _job()
    job._cg_route_ctx = _ctx(own_key=SENTINEL_KEY, state="ok")

    orig_init = _FakeGeminiEngine.__init__

    def patched(self, **kw):
        orig_init(self, **kw)
        if self.keys == [SENTINEL_KEY]:
            self.behaviour = "invalid"

    monkeypatch.setattr(_FakeGeminiEngine, "__init__", patched)
    _synth(job, monkeypatch, tmp_path)
    assert job.gemini_key_broken is True
    assert job._cg_outcome["own_key_invalid"] is True
    assert job.tts_route == "pool"
    assert not job.tts_fallback_used  # Gemini still served it


@pytestmark_ffmpeg
def test_cg_spent_key_steps_to_pool_without_marking(engines, monkeypatch, tmp_path):
    job = _job()
    job._cg_route_ctx = _ctx(own_key=SENTINEL_KEY, state="ok")
    orig_init = _FakeGeminiEngine.__init__

    def patched(self, **kw):
        orig_init(self, **kw)
        if self.keys == [SENTINEL_KEY]:
            self.behaviour = "spent"

    monkeypatch.setattr(_FakeGeminiEngine, "__init__", patched)
    _synth(job, monkeypatch, tmp_path)
    assert not job.gemini_key_broken
    assert job.tts_route == "pool"


@pytestmark_ffmpeg
def test_cg_both_routes_exhausted_keeps_ce_edge_fallback(
    engines, monkeypatch, tmp_path
):
    job = _job()
    job._cg_route_ctx = _ctx(own_key=SENTINEL_KEY, state="ok")
    orig_init = _FakeGeminiEngine.__init__

    def patched(self, **kw):
        orig_init(self, **kw)
        self.behaviour = "exhaust"

    monkeypatch.setattr(_FakeGeminiEngine, "__init__", patched)
    _synth(job, monkeypatch, tmp_path)
    assert job.tts_fallback_used is True
    assert job.tts_route == "edge_exhausted"
    # Discarded Gemini chunks debit NOTHING.
    assert job._cg_outcome["pool_requests"] == 0
    assert job._cg_outcome["pool_seconds"] == 0.0


# ── Key secrecy: never logged, never echoed ──────────────────────────

@pytestmark_ffmpeg
def test_cg_key_value_never_appears_in_logs(engines, monkeypatch, tmp_path, caplog):
    import logging

    job = _job()
    job._cg_route_ctx = _ctx(own_key=SENTINEL_KEY, state="ok")
    with caplog.at_level(logging.DEBUG):
        _synth(job, monkeypatch, tmp_path)
    assert SENTINEL_KEY not in caplog.text


def test_cg_masked_view_exposes_only_last_four():
    user = User(id=1, gemini_api_key=SENTINEL_KEY, gemini_key_state="ok")
    view = gks.masked_key_view(user)
    assert view["has_key"] is True
    assert view["masked"] == "••••" + SENTINEL_KEY[-4:]
    assert SENTINEL_KEY not in str(view)
    empty = User(id=2, gemini_api_key=None, gemini_key_state="")
    assert gks.masked_key_view(empty) == {
        "has_key": False, "masked": None, "state": None,
    }


def test_cg_route_context_repr_hides_key():
    ctx = _ctx(own_key=SENTINEL_KEY, state="ok")
    assert SENTINEL_KEY not in repr(ctx)


# ── Validation: exactly one call, invalid never stored ───────────────

def test_cg_validate_ok_key_accepted_with_one_engine_call(engines, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    accepted, reason = gks.validate_gemini_key(SENTINEL_KEY)
    assert accepted is True and reason == "ok"
    assert len(engines.instances) == 1
    assert engines.instances[0].calls == 1
    assert engines.instances[0].keys == [SENTINEL_KEY]


def test_cg_validate_invalid_key_rejected(engines, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    orig_init = _FakeGeminiEngine.__init__

    def patched(self, **kw):
        orig_init(self, **kw)
        self.behaviour = "invalid"

    monkeypatch.setattr(_FakeGeminiEngine, "__init__", patched)
    accepted, reason = gks.validate_gemini_key("bad-key-value")
    assert accepted is False and reason == "invalid"


def test_cg_validate_spent_key_still_genuine(engines, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    orig_init = _FakeGeminiEngine.__init__

    def patched(self, **kw):
        orig_init(self, **kw)
        self.behaviour = "spent"

    monkeypatch.setattr(_FakeGeminiEngine, "__init__", patched)
    accepted, reason = gks.validate_gemini_key("genuine-but-spent")
    assert accepted is True and reason == "spent"


def test_cg_validate_blank_or_spaced_key_rejected_without_a_call(engines, tmp_path):
    assert gks.validate_gemini_key("") == (False, "invalid")
    assert gks.validate_gemini_key("has space in it") == (False, "invalid")
    assert engines.instances == []


# ── Accounting: successful draws only ────────────────────────────────

@pytest_asyncio.fixture
async def _db():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.schema import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_cg_debit_counts_only_free_and_accumulates(_db):
    maker = _db
    async with maker() as session:
        await gks.debit_pool_usage(session, 777, tier="FREE", seconds=30.0, requests=2)
        await gks.debit_pool_usage(session, 777, tier="FREE", seconds=15.0, requests=1)
        # PURCHASED pool use is never debited.
        await gks.debit_pool_usage(session, 888, tier="PURCHASED", seconds=99.0, requests=9)
        await session.commit()
        assert await gks.taste_used_seconds(session, 777) == pytest.approx(45.0)
        assert await gks.free_budget_used_requests(session) == 3
        rows = (await session.execute(select(GeminiPoolUsage))).scalars().all()
        assert len(rows) == 1 and rows[0].user_id == 777


@pytest.mark.asyncio
async def test_cg_resolve_route_reads_taste_and_key_state(_db, monkeypatch):
    monkeypatch.setattr(settings, "free_gemini_taste_minutes", 2)
    monkeypatch.setattr(settings, "free_gemini_daily_budget_requests", 100)
    maker = _db
    async with maker() as session:
        user = User(
            id=777,
            account_category=AccountCategory.FREE.value,
            gemini_api_key=SENTINEL_KEY,
            gemini_key_state="invalid",
        )
        session.add(user)
        await gks.debit_pool_usage(session, 777, tier="FREE", seconds=130.0, requests=4)
        await session.commit()
        ctx = await gks.resolve_gemini_route(session, user, is_admin=False)
        assert ctx.taste_limit_seconds == 120.0
        assert ctx.taste_used_seconds == pytest.approx(130.0)
        assert ctx.has_usable_own_key is False  # marked invalid
        assert ctx.pool_allowed is False
        assert ctx.taste_blocked is True
        assert ctx.taste_remaining_seconds == 0.0


# ── Executor end-to-end: debit + marking + caps interplay ────────────

@pytest_asyncio.fixture
async def _exec_db(monkeypatch, tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import session as session_mod
    from app.models.schema import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "bot_token", "")
    yield maker
    await engine.dispose()


@pytestmark_ffmpeg
@pytest.mark.asyncio
async def test_cg_executor_free_pool_job_debits_taste(_exec_db, engines):
    maker = _exec_db
    from app.services.ledger import get_or_create_user

    async with maker() as session:
        user, _ = await get_or_create_user(session, 909101)
        await session.commit()
        job, reason = await tts_service.create_tts_job(
            session, user.id, text="မင်္ဂလာပါ။", engine="gemini",
            voice="Kore", speed_rate="+12%",
        )
        await session.commit()
        assert job is not None, reason
        job_id, user_id = job.id, user.id

    await tts_service.execute_tts_job(job_id)

    async with maker() as session:
        row = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert row.status == "SUCCESS"
        assert row.tts_route == "pool"
        assert await gks.taste_used_seconds(session, user_id) == pytest.approx(
            float(row.seconds_consumed), abs=1.5
        )
        assert await gks.free_budget_used_requests(session) >= 1


@pytestmark_ffmpeg
@pytest.mark.asyncio
async def test_cg_executor_own_key_job_counts_toward_day_cap(_exec_db, engines, monkeypatch):
    maker = _exec_db
    from app.services.ledger import get_or_create_user

    async with maker() as session:
        user, _ = await get_or_create_user(session, 909102)
        user.gemini_api_key = SENTINEL_KEY
        user.gemini_key_state = "ok"
        await session.commit()
        job, reason = await tts_service.create_tts_job(
            session, user.id, text="မင်္ဂလာပါ။", engine="gemini",
            voice="Kore", speed_rate="+12%",
        )
        await session.commit()
        assert job is not None, reason
        job_id, user_id = job.id, user.id

    await tts_service.execute_tts_job(job_id)

    async with maker() as session:
        row = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert row.status == "SUCCESS"
        assert row.tts_route == "own_key"
        # Caps interplay: the own-key minutes still count toward the
        # 15-min TTS day (seconds_consumed is engine-agnostic).
        consumed = await tts_service.tts_cap_consumed_seconds(session, user_id)
        assert consumed == pytest.approx(float(row.seconds_consumed))
        # ...but the pool funded nothing.
        assert await gks.taste_used_seconds(session, user_id) == 0.0


@pytestmark_ffmpeg
@pytest.mark.asyncio
async def test_cg_executor_broken_key_marked_and_user_flagged(_exec_db, engines):
    maker = _exec_db
    from app.services.ledger import get_or_create_user

    orig_init = _FakeGeminiEngine.__init__

    def patched(self, **kw):
        orig_init(self, **kw)
        if self.keys == [SENTINEL_KEY]:
            self.behaviour = "invalid"

    async with maker() as session:
        user, _ = await get_or_create_user(session, 909103)
        user.gemini_api_key = SENTINEL_KEY
        user.gemini_key_state = "ok"
        await session.commit()
        job, reason = await tts_service.create_tts_job(
            session, user.id, text="မင်္ဂလာပါ။", engine="gemini",
            voice="Kore", speed_rate="+12%",
        )
        await session.commit()
        assert job is not None, reason
        job_id, user_id = job.id, user.id

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_FakeGeminiEngine, "__init__", patched)
        await tts_service.execute_tts_job(job_id)

    async with maker() as session:
        row = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert row.status == "SUCCESS"
        assert row.gemini_key_broken is True
        assert row.tts_route == "pool"
        owner = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        assert owner.gemini_key_state == "invalid"


# ── Endpoints: masked-only payloads, invalid never stored ────────────

@pytest.mark.asyncio
async def test_cg_key_endpoints_mask_and_store(monkeypatch, _db):
    from app.webapp import api as api_mod

    maker = _db
    async with maker() as session:
        user = User(id=31337, account_category=AccountCategory.FREE.value)
        session.add(user)
        await session.commit()

        # Unverifiable/invalid → HTTP 400, nothing stored.
        monkeypatch.setattr(
            gks, "validate_gemini_key", lambda c: (False, "invalid")
        )
        with pytest.raises(api_mod.HTTPException) as excinfo:
            await api_mod.save_tts_gemini_key(
                api_mod.GeminiKeyPayload(key="bad"), user=user, db=session, _gate=user
            )
        assert excinfo.value.status_code == 400
        assert "bad" not in str(excinfo.value.detail)
        await session.refresh(user)
        assert user.gemini_api_key is None

        # Accepted → stored, response carries the masked view ONLY.
        monkeypatch.setattr(gks, "validate_gemini_key", lambda c: (True, "ok"))
        out = await api_mod.save_tts_gemini_key(
            api_mod.GeminiKeyPayload(key=SENTINEL_KEY), user=user, db=session, _gate=user
        )
        assert SENTINEL_KEY not in str(out)
        assert out["masked"] == "••••" + SENTINEL_KEY[-4:]
        await session.refresh(user)
        assert user.gemini_api_key == SENTINEL_KEY
        assert user.gemini_key_state == "ok"

        # Delete clears immediately.
        out = await api_mod.delete_tts_gemini_key(user=user, db=session, _gate=user)
        assert out["has_key"] is False
        await session.refresh(user)
        assert user.gemini_api_key is None


@pytest.mark.asyncio
async def test_cg_quota_carries_taste_and_key_fields(monkeypatch, _db):
    from app.webapp import api as api_mod

    maker = _db
    monkeypatch.setattr(settings, "free_gemini_taste_minutes", 2)
    async with maker() as session:
        user = User(
            id=31338,
            account_category=AccountCategory.FREE.value,
            gemini_api_key=SENTINEL_KEY,
            gemini_key_state="ok",
        )
        session.add(user)
        await session.commit()
        out = await api_mod.get_tts_quota(user=user, db=session, _gate=user)
        assert out["has_own_key"] is True
        assert out["own_key_masked"] == "••••" + SENTINEL_KEY[-4:]
        assert SENTINEL_KEY not in str(out)
        assert out["gemini_taste_limit_seconds"] == 120
        assert out["gemini_taste_remaining_seconds"] == 120


# ── Recap path shares the routing ────────────────────────────────────

def test_cg_recap_chain_passes_route_as_attribute_not_overlays():
    core_src = (ROOT / "app/services/recap_engine/core.py").read_text(encoding="utf-8")
    assert "gemini_route" in core_src
    assert "self.gemini_route = gemini_route" in core_src
    pipe_src = (ROOT / "app/services/recap_engine/recap_pipeline.py").read_text(encoding="utf-8")
    assert "gemini_route=gemini_route" in pipe_src
    dl_src = (ROOT / "app/services/downloader.py").read_text(encoding="utf-8")
    assert "gemini_route=gemini_route" in dl_src
    api_src = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
    assert "gemini_route=_cg_route_ctx" in api_src
    # The route context must never ride the persisted overlays dict.
    assert 'overlays["gemini_route"]' not in core_src


def test_cg_routed_engine_own_then_pool_stats():
    own = _FakeGeminiEngine(ffmpeg="ffmpeg", work_dir=Path("/tmp"), keys=["k1"])
    pool = _FakeGeminiEngine(ffmpeg="ffmpeg", work_dir=Path("/tmp"), keys=["k2"])
    own.behaviour = "spent"
    routed = gte.RoutedGeminiEngine(own, pool, log=lambda m: None)
    out = routed.generate_channel99_voice("hello", "Kore")
    assert out.is_file()
    assert routed.pool_requests == 1 and routed.own_requests == 0
    assert routed.own_key_invalid is False


def test_cg_routed_engine_invalid_flags_and_pool_serves():
    own = _FakeGeminiEngine(ffmpeg="ffmpeg", work_dir=Path("/tmp"), keys=["k1"])
    pool = _FakeGeminiEngine(ffmpeg="ffmpeg", work_dir=Path("/tmp"), keys=["k2"])
    own.behaviour = "invalid"
    routed = gte.RoutedGeminiEngine(own, pool, log=lambda m: None)
    routed.generate_channel99_voice("hello", "Kore")
    assert routed.own_key_invalid is True
    assert routed.pool_requests == 1


# ── Settings, fact sheet, locales, UI pins ───────────────────────────

def test_cg_settings_registered_with_owner_defaults():
    from app.services.dynamic_settings import SETTING_TYPES

    assert SETTING_TYPES["free_gemini_taste_minutes"] is int
    assert SETTING_TYPES["free_gemini_daily_budget_requests"] is int
    assert settings.free_gemini_taste_minutes == 2
    assert settings.free_gemini_daily_budget_requests == 100


def test_cg_fact_sheet_states_byok_facts():
    from app.services.group_ai_admin import build_fact_sheet

    sheet = build_fact_sheet()
    assert "Gemini API Key" in sheet
    assert "TTS Tab" in sheet


def test_cg_bot_locales_have_all_new_keys():
    import json

    for lang in ("my", "en"):
        data = json.loads(
            (ROOT / f"locales/{lang}/messages.json").read_text(encoding="utf-8")
        )
        for key in (
            "tts_taste_key_hint",
            "tts_own_key_broken_note",
            "recap_key_hint_note",
            "recap_own_key_broken_note",
            "gemini_key_invalid_error",
            "gemini_key_unverifiable_error",
        ):
            assert data.get(key), f"{lang}:{key}"


def test_cg_webapp_key_card_prompt_and_i18n_exist():
    html = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
    assert 'id="tts-gemini-key-card"' in html
    assert 'id="tts-key-prompt-modal"' in html
    assert 'id="tts-taste-note"' in html
    assert 'id="adm-set-free-gemini-taste"' in html
    assert 'id="adm-set-free-gemini-budget"' in html
    js = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
    for key in (
        "tts_key_card_title", "tts_key_prompt_title", "tts_key_prompt_add",
        "tts_key_prompt_edge", "tts_taste_remaining", "tts_taste_spent",
        "settings_free_gemini_taste_minutes", "settings_free_gemini_budget_requests",
    ):
        assert js.count(f"{key}:") >= 2, key  # my + en
    app_js = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
    assert "_cgTasteBlocked" in app_js
    assert "/api/tts/gemini-key" in app_js
    # The typed key value is never re-rendered: the card paints only
    # the server's masked field.
    assert "own_key_masked" in app_js


def test_cg_schema_and_migrations_carry_key_columns():
    schema_src = (ROOT / "app/models/schema.py").read_text(encoding="utf-8")
    assert "gemini_api_key" in schema_src
    assert "gemini_key_state" in schema_src
    assert "tts_route" in schema_src
    assert "class GeminiPoolUsage" in schema_src
    session_src = (ROOT / "app/db/session.py").read_text(encoding="utf-8")
    assert session_src.count("gemini_api_key") >= 2  # PG + SQLite
    assert session_src.count("tts_route") >= 2


def test_cg_route_marker_format():
    import logging

    records = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    lg = logging.getLogger("app.services.gemini_key_service")
    lg.addHandler(_H())
    lg.setLevel(logging.INFO)
    try:
        gks.log_route("own_key", 4242, "FREE")
    finally:
        lg.handlers.pop()
    assert records == ["TTS route: own_key user=4242 tier=FREE"]


def test_cg_cache_token_is_r104():
    html = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
    assert html.count("20261116_r158") == 4
    assert ("20261104_r" + "105") not in html
