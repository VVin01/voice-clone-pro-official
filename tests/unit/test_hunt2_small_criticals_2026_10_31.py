"""Hunt #2 small criticals (branch fix-toast-start-2026-10-31).

X1  Stored XSS into the ADMIN Mini App via preset_voice: the value was
    stored verbatim on the Job row and later rendered into admin
    onclick="…" JS-string attributes with only double quotes escaped,
    so a payload like  x');alert(1);//  broke out. Fixed on both
    sides: a server-side slug allowlist at job creation (voice +
    recap), and the two admin Play/Preview buttons now pass job data
    through data-* attributes + addEventListener.

V2  Personal voice samples/models were exposed: GET
    /api/voices/sample/{slug} had NO auth and served any model's
    sample; GET /api/voices/models listed other users' PERSONAL
    models to any signed-in user. The sample endpoint now requires a
    signed-in user and, for PERSONAL models, ownership or a grant;
    the models list hides other users' personal models. The Mini App
    sample players were switched to authenticated blob loading (an
    <audio src> / Audio() load cannot send the initData header).

R1  Recap double-submit: Analyze/Upload success re-enabled the Start
    button even while a recap job was actively polling, so a second
    submit charged a second credit and orphaned the first job. Both
    re-enable sites are now guarded by recapJobIsActive() and
    handleStartRecap itself refuses to start while a job is active.

R4  Admin balance-adjust had no in-flight guard: a double-tap on
    Apply credited/debited twice. The button is now disabled for the
    duration of the request (mirroring submitPaymentSlip).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from app.config import settings
from app.models.enums import AccountCategory, VoiceModelStatus, VoiceModelType
from app.models.schema import Job, UserVoiceModelGrant, VoiceModel
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
API_PY = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")

XSS_PAYLOAD = "x');alert(1);//"


async def _make_user(db_session, telegram_id, **fields):
    user, _ = await get_or_create_user(db_session, telegram_id=telegram_id)
    for key, value in fields.items():
        setattr(user, key, value)
    await db_session.commit()
    return user


async def _make_model(db_session, slug, type_, owner_id=None, sample_path=None):
    m = VoiceModel(
        name=slug,
        slug=slug,
        type=type_,
        owner_user_id=owner_id,
        model_dir="cloud",
        pth_path="voxcpm",
        sample_audio_path=sample_path,
        status=VoiceModelStatus.ACTIVE.value,
    )
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(m)
    return m


# ── X1: preset_voice / voice_slug allowlist (server) ─────────────────────

def test_x1_allowlist_helper_semantics():
    # None = "no preset" and stays allowed; curated slugs (incl. the
    # voxcpm_ prefix the resolver strips) pass; everything else 400s.
    api_mod._require_valid_preset_voice(None)
    api_mod._require_valid_preset_voice("komin")
    api_mod._require_valid_preset_voice("voxcpm_mahnin")
    for bad in (XSS_PAYLOAD, "not-a-voice", "", "komin'--"):
        with pytest.raises(HTTPException) as exc:
            api_mod._require_valid_preset_voice(bad)
        assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_x1_voice_job_payload_preset_voice_rejected(db_session):
    user = await _make_user(db_session, 881001)
    req = api_mod.VoiceGenerateRequest(text="မင်္ဂလာပါ", preset_voice=XSS_PAYLOAD)
    with pytest.raises(HTTPException) as exc:
        await api_mod.create_voice_job(req, user=user, db=db_session, _gate=user)
    assert exc.value.status_code == 400
    # Nothing was stored: no job row exists at all.
    count = (await db_session.execute(select(func.count()).select_from(Job))).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_x1_voice_job_curated_slug_accepted_and_stored(db_session):
    user = await _make_user(db_session, 881002)
    req = api_mod.VoiceGenerateRequest(text="မင်္ဂလာပါ", preset_voice="komin")
    resp = await api_mod.create_voice_job(req, user=user, db=db_session, _gate=user)
    assert resp["success"] is True
    assert resp["preset_voice"] == "komin"
    job = await db_session.get(Job, resp["job_id"])
    assert job.preset_voice == "komin"


@pytest.mark.asyncio
async def test_x1_voice_job_with_audio_payload_rejected(db_session):
    user = await _make_user(db_session, 881003)
    with pytest.raises(HTTPException) as exc:
        await api_mod.create_voice_job_with_audio(
            text="မင်္ဂလာပါ",
            model_slug="voxcpm-cloud-ai",
            engine="voxcpm",
            preset_voice=XSS_PAYLOAD,
            reference_audio=None,
            user=user,
            db=db_session,
            _gate=user,
        )
    assert exc.value.status_code == 400


@pytest.fixture
def recap_runner(monkeypatch):
    """Capture background-job dispatches instead of running the pipeline."""
    calls = []

    async def _fake_runner(job_id, req, user_id, chat_id, **kwargs):
        calls.append({"job_id": job_id, "req": req, "user_id": user_id, "kwargs": kwargs})

    monkeypatch.setattr(api_mod, "_run_recap_background_job", _fake_runner)
    yield calls
    api_mod.RECAP_JOBS_REGISTRY.clear()


def _recap_req(**over):
    base = {"url": "", "title": "Test Movie", "voice_slug": "komin", "duration_seconds": 60}
    base.update(over)
    return api_mod.RecapConfigRequest(**base)


@pytest.mark.asyncio
async def test_x1_recap_payload_voice_slug_rejected_before_charge(db_session, recap_runner):
    user = await _make_user(db_session, 881011, recap_credits=5)
    with pytest.raises(HTTPException) as exc:
        await api_mod.create_recap_job(
            _recap_req(voice_slug=XSS_PAYLOAD), user=user, db=db_session, _gate=user
        )
    assert exc.value.status_code == 400
    await db_session.refresh(user)
    assert user.recap_credits == 5  # rejected before any deduction
    assert recap_runner == []


@pytest.mark.asyncio
async def test_x1_recap_curated_and_legacy_slugs_accepted(db_session, recap_runner):
    for i, slug in enumerate(("mahnin", "nilar")):
        user = await _make_user(db_session, 881020 + i, recap_credits=3)
        resp = await api_mod.create_recap_job(
            _recap_req(voice_slug=slug), user=user, db=db_session, _gate=user
        )
        await asyncio.sleep(0)
        assert resp["success"] is True
        job = await db_session.get(Job, resp["recap_id"])
        assert job.preset_voice == slug


# ── X1: admin media buttons no longer build onclick JS strings ───────────

def test_x1_admin_media_buttons_use_data_attributes():
    # The two vulnerable interpolations are gone entirely.
    assert 'onclick="openAdminMediaModal(' not in APP_JS
    assert "• ${j.preset_voice || j.engine}`.replace" not in APP_JS
    # Both buttons (queue Play + history Preview) now carry data-* …
    assert APP_JS.count("adm-media-open-btn") == 3  # 2 buttons + wiring selector
    assert APP_JS.count('data-deliverables="${escapeHtml(JSON.stringify(j.deliverables || {}))}"') == 2
    assert APP_JS.count('data-title="${escapeHtml(') == 2
    # … and are wired after each render.
    assert "function wireAdminMediaButtons" in APP_JS
    assert APP_JS.count("wireAdminMediaButtons(container);") == 2


# ── V2: sample endpoint auth + personal-model guard ──────────────────────

def test_v2_sample_route_requires_current_user():
    route = next(
        r for r in api_mod.api_router.routes
        if getattr(r, "path", "") == "/api/voices/sample/{slug}"
    )
    calls = set()

    def _collect(dependant):
        for dep in dependant.dependencies:
            if dep.call is not None:
                calls.add(dep.call)
            _collect(dep)

    _collect(route.dependant)
    assert api_mod.get_current_user in calls


@pytest.mark.asyncio
async def test_v2_other_users_personal_sample_denied(db_session, tmp_path):
    owner = await _make_user(db_session, 881101)
    stranger = await _make_user(db_session, 881102)
    sample = tmp_path / "personal_sample.wav"
    sample.write_bytes(b"RIFF-fake")
    await _make_model(
        db_session, "personal-secret-voice", VoiceModelType.PERSONAL.value,
        owner_id=owner.id, sample_path=str(sample),
    )
    with pytest.raises(HTTPException) as exc:
        await api_mod.get_voice_sample_audio(
            "personal-secret-voice", user=stranger, db=db_session
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_v2_owner_and_grantee_can_hear_personal_sample(db_session, tmp_path):
    owner = await _make_user(
        db_session, 881111, account_category=AccountCategory.PURCHASED.value
    )
    grantee = await _make_user(
        db_session, 881112, account_category=AccountCategory.PURCHASED.value
    )
    sample = tmp_path / "personal_sample2.wav"
    sample.write_bytes(b"RIFF-fake")
    model = await _make_model(
        db_session, "personal-shared-voice", VoiceModelType.PERSONAL.value,
        owner_id=owner.id, sample_path=str(sample),
    )

    resp = await api_mod.get_voice_sample_audio(
        "personal-shared-voice", user=owner, db=db_session
    )
    assert isinstance(resp, FileResponse)
    assert resp.path == str(sample)

    db_session.add(UserVoiceModelGrant(user_id=grantee.id, model_id=model.id))
    await db_session.commit()
    resp = await api_mod.get_voice_sample_audio(
        "personal-shared-voice", user=grantee, db=db_session
    )
    assert isinstance(resp, FileResponse)
    assert resp.path == str(sample)


@pytest.mark.asyncio
async def test_v2_curated_and_system_samples_open_to_signed_in_users(db_session, tmp_path):
    user = await _make_user(db_session, 881121)
    # Curated persona sample (static file) — any signed-in user.
    resp = await api_mod.get_voice_sample_audio("komin", user=user, db=db_session)
    assert isinstance(resp, FileResponse)
    assert resp.path.endswith("komin.mp3")

    # SYSTEM model sample from the DB — any signed-in user.
    sample = tmp_path / "system_sample.wav"
    sample.write_bytes(b"RIFF-fake")
    await _make_model(
        db_session, "system-extra-voice", VoiceModelType.SYSTEM.value,
        sample_path=str(sample),
    )
    resp = await api_mod.get_voice_sample_audio(
        "system-extra-voice", user=user, db=db_session
    )
    assert isinstance(resp, FileResponse)
    assert resp.path == str(sample)


# ── V2: models list hides other users' personal models ───────────────────

@pytest.mark.asyncio
async def test_v2_models_list_scopes_personal_models(db_session):
    owner = await _make_user(db_session, 881131)
    other = await _make_user(db_session, 881132)
    await _make_model(db_session, "sys-voice-a", VoiceModelType.SYSTEM.value)
    await _make_model(
        db_session, "owner-personal-a", VoiceModelType.PERSONAL.value, owner_id=owner.id
    )

    other_slugs = {m["slug"] for m in await api_mod.list_voice_models(user=other, db=db_session)}
    assert "sys-voice-a" in other_slugs
    assert "owner-personal-a" not in other_slugs

    owner_slugs = {m["slug"] for m in await api_mod.list_voice_models(user=owner, db=db_session)}
    assert "sys-voice-a" in owner_slugs
    assert "owner-personal-a" in owner_slugs


def test_v2_frontend_loads_samples_with_auth():
    # Audio() / <audio src> cannot send the initData header, so both
    # preview paths fetch the sample with auth and play a blob URL.
    assert "await fetchAuthImageUrl(audioUrl)" in APP_JS
    assert 'getAttribute("data-sample-url")' in APP_JS


# ── R1: recap double-submit guards ───────────────────────────────────────

def test_r1_start_recap_refuses_while_job_active():
    start = APP_JS.index("async function handleStartRecap()")
    end = APP_JS.index("function startRecapJobPolling")
    assert "if (recapJobIsActive()) return;" in APP_JS[start:end]


def test_r1_reenable_sites_are_guarded():
    # Exactly two btn-start-recap re-enables exist outside the
    # submit/poll lifecycle (Analyze + Upload success) and both are
    # guarded by the active-job check.
    assert APP_JS.count("btnStartRecap.disabled = false") == 2
    assert APP_JS.count("btnStartRecap && !recapJobIsActive()") == 2


def test_r1_active_helper_reflects_poll_timer():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    start = APP_JS.index("function recapJobIsActive()")
    end = APP_JS.index("async function handleStartRecap()")
    helper_src = APP_JS[start:end]
    script = (
        "var state = { recapPollTimer: null };\n"
        + helper_src
        + "\nconst out = { inactive: recapJobIsActive() };\n"
        + "state.recapPollTimer = 123;\n"
        + "out.active = recapJobIsActive();\n"
        + "console.log(JSON.stringify(out));\n"
    )
    tmp = ROOT / "tests/unit/_hunt2_harness_tmp.js"
    try:
        tmp.write_text(script, encoding="utf-8")
        res = subprocess.run([node, str(tmp)], capture_output=True, text=True, timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out == {"inactive": False, "active": True}


# ── R4: admin adjust in-flight guard ─────────────────────────────────────

def test_r4_adjust_disables_apply_button_until_finally():
    start = APP_JS.index("async function adminApplyAdjust()")
    end = APP_JS.index("window.adminApplyAdjust = adminApplyAdjust;")
    body = APP_JS[start:end]
    assert 'document.getElementById("adm-adjust-apply-btn")' in body
    assert "applyBtn.disabled = true" in body
    assert "applyBtn.disabled = false" in body
    # Disabled before the request goes out, restored in a finally
    # after the catch — the submitPaymentSlip pattern.
    assert body.index("applyBtn.disabled = true") < body.index('fetch("/api/admin/adjust-user"')
    assert "} finally {" in body
    assert body.index("applyBtn.disabled = false") > body.index("} finally {")
