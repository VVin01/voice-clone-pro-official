"""Round 6 regression pins (2026-11-21):

- Items 1–4: Voice Tab cleanup (#custom-clone-stepper removed,
  custom_ref_optional hint removed, voice_step2_select parenthetical dropped,
  btn_mode_curated simplified to "🌟 Persona").
- Item 5: Backend live-job tracker reconciliation with the shared ``jobs``
  table so terminal states (SUCCESS / FAILED / CANCELLED) from worker
  processes (RECAP_ON_QUEUE=1), stranded failures, or cross-process cancels
  never leave phantom active entries blocking Step 4 submit or causing
  contradictory 409 Cancel errors — while leaving legitimately QUEUED
  (RECAP_EXECUTION_SEMAPHORE) jobs untouched.
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.config import settings
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Job
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod
from app import worker as worker_mod

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")


def _recap_req(**over) -> api_mod.RecapConfigRequest:
    base = dict(
        url="https://example.com/unplannable-movie",
        title="Unplannable Test Movie",
        voice_slug="mahnin",
        tts_engine="edge",
        story_style="summary",
        ratio="9:16",
    )
    base.update(over)
    return api_mod.RecapConfigRequest(**base)


def _translations() -> dict:
    proc = subprocess.run(
        [
            "node",
            "-e",
            "const fs=require('fs'),vm=require('vm');"
            "const c={window:{},localStorage:{getItem:()=>null,setItem:()=>{}}};"
            "vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8')+';this._T=translations;',c);"
            "console.log(JSON.stringify(c._T));",
            str(ROOT / "app/webapp/static/js/i18n.js"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(proc.stdout)


# ── Items 1–4: Voice Tab Round 6 UI pins ───────────────────────────────

def test_round6_item1_custom_clone_stepper_removed_and_section_order():
    assert 'id="custom-clone-stepper"' not in INDEX_HTML
    assert "data-clone-step" not in INDEX_HTML
    assert "vcp-clone-steps-bar" not in INDEX_HTML
    assert "custom-clone-stepper" not in APP_JS
    assert "data-clone-step" not in APP_JS
    assert ".vcp-clone-steps-bar" not in STYLE_CSS
    assert ".vcp-clone-step" not in STYLE_CSS

    tr = _translations()
    for removed_key in (
        "clone_step_1_upload",
        "clone_step_2_check",
        "clone_step_3_create",
        "clone_step_4_listen",
        "clone_step_5_save",
    ):
        assert removed_key not in tr["my"]
        assert removed_key not in tr["en"]
    # Save button key and remaining feedback surfaces stay intact.
    assert "clone_step5_save_btn" in tr["my"] and "clone_step5_save_btn" in tr["en"]
    assert 'id="saved-custom-voice-bar"' in INDEX_HTML
    assert 'id="custom-clone-check-status"' in INDEX_HTML

    # Final order inside #ref-audio-section:
    # title row -> saved-voice bar -> upload row -> collapsed notes -> check status.
    sec = INDEX_HTML.split('id="ref-audio-section"', 1)[1].split("<!-- Step 2:", 1)[0]
    i_title = sec.index('data-i18n="custom_ref_title"')
    i_saved = sec.index('id="saved-custom-voice-bar"')
    i_upload = sec.index('class="ref-upload-row"')
    i_notes = sec.index('id="ref-notes-details"')
    i_check = sec.index('id="custom-clone-check-status"')
    assert i_title < i_saved < i_upload < i_notes < i_check


def test_round6_item2_optional_hint_tag_removed():
    assert "custom_ref_optional" not in INDEX_HTML
    tr = _translations()
    assert "custom_ref_optional" not in tr["my"]
    assert "custom_ref_optional" not in tr["en"]


def test_round6_item3_step2_title_parenthetical_dropped():
    tr = _translations()
    assert tr["my"]["voice_step2_select"] == "အသံ ရွေးချယ်ရန်"
    assert tr["en"]["voice_step2_select"] == "Select Voice"
    assert 'data-i18n="voice_step2_select">အသံ ရွေးချယ်ရန်</span>' in INDEX_HTML


def test_round6_item4_persona_button_simplified():
    tr = _translations()
    assert tr["my"]["btn_mode_curated"] == "🌟 Persona"
    assert tr["en"]["btn_mode_curated"] == "🌟 Persona"
    assert 'data-i18n="btn_mode_curated">🌟 Persona</span>' in INDEX_HTML
    # Custom voice seg button unchanged.
    assert tr["my"]["btn_mode_custom"] == "🎙️ ကိုယ်ပိုင်အသံ"
    assert tr["en"]["btn_mode_custom"] == "🎙️ My Voice"


# ── Item 5: Backend live-job reconciliation & self-healing ─────────────

@pytest.mark.asyncio
async def test_round6_item5_submit_fail_on_worker_reconciles_and_allows_resubmit(
    db_session, monkeypatch
):
    """Exact reproduction of rc_1795259362_2ae971 (2026-11-21):

    1. Web process submits recap job with RECAP_ON_QUEUE=1 -> RAM has QUEUED 0/4.
    2. Separate worker process fails the job in DB -> jobs.status = FAILED,
       while web process RAM still holds QUEUED 0/4.
    3. /api/jobs/live reports active_count == 0 (never active), self-heals RAM,
       both within 10 minutes AND > 10 minutes past finished_at.
    4. /api/recap/{id}/status returns FAILED (not QUEUED).
    5. _recap_active_job_count == 0 and Cancel returns 409 consistently.
    6. A new recap submission is accepted with no admin retry and no restart.
    """
    admin, _ = await get_or_create_user(db_session, telegram_id=996001)
    monkeypatch.setattr(settings, "admin_ids_raw", str(admin.id))
    monkeypatch.setattr(settings, "recap_on_queue", True)

    resp1 = await api_mod.create_recap_job(
        _recap_req(), user=admin, db=db_session, _gate=admin
    )
    assert resp1["success"] is True
    jid1 = resp1["recap_id"]

    try:
        # Sanity: while genuinely QUEUED in DB, job IS active (semaphore untouched).
        live_q = await api_mod.get_live_jobs(user=admin, db=db_session)
        assert live_q["active_count"] == 1
        assert live_q["jobs"][0]["id"] == jid1
        assert live_q["jobs"][0]["status"] == JobStatus.QUEUED.value
        assert await api_mod._recap_active_job_count(db_session, admin.id) == 1

        # Simulate worker process failing the job in DB WITHOUT touching web RAM!
        db_job = await db_session.get(Job, jid1)
        assert db_job is not None
        db_job.status = JobStatus.FAILED.value
        db_job.error_message = "Source video cannot be planned"
        db_job.finished_at = datetime.now(timezone.utc)
        await db_session.commit()

        # Web RAM is still stale QUEUED 0/4 before reconciliation.
        assert api_mod.RECAP_JOBS_REGISTRY[jid1]["status"] == "QUEUED"
        assert api_mod.RECAP_JOBS_REGISTRY[jid1]["progress_step"] == "0/4"

        # (a) /api/jobs/live within 10-min terminal window: reports FAILED, active_count == 0!
        live_after_fail = await api_mod.get_live_jobs(user=admin, db=db_session)
        assert live_after_fail["active_count"] == 0
        matched = [j for j in live_after_fail["jobs"] if j["id"] == jid1]
        assert len(matched) == 1
        assert matched[0]["status"] == JobStatus.FAILED.value
        assert matched[0]["queue_position"] is None
        assert api_mod.RECAP_JOBS_REGISTRY[jid1]["status"] == JobStatus.FAILED.value

        # (b) Simulate > 10 min past finished_at with stale RAM reset back to QUEUED:
        # primary DB query skips old terminal row -> batch self-heal MUST still reconcile
        # and NEVER emit a phantom active entry!
        api_mod.RECAP_JOBS_REGISTRY[jid1]["status"] = "QUEUED"
        api_mod.RECAP_JOBS_REGISTRY[jid1]["progress_step"] = "0/4"
        old_ts = datetime.now(timezone.utc) - timedelta(minutes=30)
        db_job.created_at = old_ts - timedelta(minutes=5)
        db_job.finished_at = old_ts
        await db_session.commit()

        live_old = await api_mod.get_live_jobs(user=admin, db=db_session)
        assert live_old["active_count"] == 0
        assert all(j["id"] != jid1 for j in live_old["jobs"])
        assert api_mod.RECAP_JOBS_REGISTRY[jid1]["status"] == JobStatus.FAILED.value

        # (c) /api/recap/{id}/status with stale QUEUED RAM -> reconciles and returns FAILED.
        api_mod.RECAP_JOBS_REGISTRY[jid1]["status"] = "QUEUED"
        status_res = await api_mod.get_recap_status(jid1, user=admin, db=db_session)
        assert status_res["status"] == JobStatus.FAILED.value
        assert status_res["error_message"] == "Source video cannot be planned"
        assert api_mod.RECAP_JOBS_REGISTRY[jid1]["status"] == JobStatus.FAILED.value

        # (d) _recap_active_job_count with stale QUEUED RAM -> reconciles and returns 0.
        api_mod.RECAP_JOBS_REGISTRY[jid1]["status"] = "QUEUED"
        assert await api_mod._recap_active_job_count(db_session, admin.id) == 0
        assert api_mod.RECAP_JOBS_REGISTRY[jid1]["status"] == JobStatus.FAILED.value

        # (e) Cancel consistency: terminal job is NOT presented active and returns 409,
        # while also self-healing any stale RAM entry.
        api_mod.RECAP_JOBS_REGISTRY[jid1]["status"] = "QUEUED"
        with pytest.raises(HTTPException) as exc_cancel:
            await api_mod.cancel_recap_job(jid1, user=admin, db=db_session)
        assert exc_cancel.value.status_code == 409
        assert api_mod.RECAP_JOBS_REGISTRY[jid1]["status"] == JobStatus.FAILED.value

        # (f) Resubmitting a brand-new recap job succeeds immediately with no admin retry
        # and no restart!
        resp2 = await api_mod.create_recap_job(
            _recap_req(title="Fresh Second Recap"), user=admin, db=db_session, _gate=admin
        )
        assert resp2["success"] is True
        jid2 = resp2["recap_id"]
        assert jid2 != jid1
        live2 = await api_mod.get_live_jobs(user=admin, db=db_session)
        assert live2["active_count"] == 1
        assert live2["jobs"][0]["id"] == jid2
        assert live2["jobs"][0]["status"] == JobStatus.QUEUED.value

        # Clean up second job's registry entry.
        api_mod.RECAP_JOBS_REGISTRY.pop(jid2, None)
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(jid1, None)


@pytest.mark.asyncio
async def test_round6_item5_worker_exception_marks_db_and_registry_failed(
    db_session, monkeypatch
):
    """If worker execution raises unexpectedly, process_recap_job guarantees
    DB terminal FAILED + reconciled registry so live-jobs shows 0 active."""
    user, _ = await get_or_create_user(db_session, telegram_id=996002)
    user.account_category = AccountCategory.PURCHASED.value
    user.recap_credits = 5
    await db_session.commit()
    monkeypatch.setattr(settings, "recap_on_queue", True)

    resp1 = await api_mod.create_recap_job(
        _recap_req(tts_engine="edge"), user=user, db=db_session, _gate=user
    )
    jid1 = resp1["recap_id"]
    db_job = await db_session.get(Job, jid1)
    assert db_job is not None

    async def _boom(*args, **kwargs):
        raise RuntimeError("unplannable source exploded before internal handler")

    monkeypatch.setattr(api_mod, "_execute_recap_background_job", _boom)

    try:
        ok = await worker_mod.process_recap_job(db_session, db_job)
        assert ok is False
        await db_session.refresh(db_job)
        assert db_job.status == JobStatus.FAILED.value
        assert db_job.finished_at is not None
        assert api_mod.RECAP_JOBS_REGISTRY[jid1]["status"] == JobStatus.FAILED.value

        live = await api_mod.get_live_jobs(user=user, db=db_session)
        assert live["active_count"] == 0
        assert await api_mod._recap_active_job_count(db_session, user.id) == 0

        # New submission accepted right away.
        resp2 = await api_mod.create_recap_job(
            _recap_req(tts_engine="edge", title="Second Try"),
            user=user,
            db=db_session,
            _gate=user,
        )
        assert resp2["success"] is True
        api_mod.RECAP_JOBS_REGISTRY.pop(resp2["recap_id"], None)
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(jid1, None)


@pytest.mark.asyncio
async def test_round6_item5_success_and_cancelled_worker_desync_reconcile(
    db_session,
):
    """Both SUCCESS and CANCELLED DB terminal rows override stale QUEUED RAM."""
    user, _ = await get_or_create_user(db_session, telegram_id=996003)
    now = datetime.now(timezone.utc)
    jid_ok = "rc_r6_worker_ok"
    jid_cancel = "rc_r6_worker_cancel"

    db_session.add(
        Job(
            id=jid_ok,
            user_id=user.id,
            chat_id=user.id,
            status=JobStatus.SUCCESS.value,
            job_type=JobType.RECAP.value,
            engine="recap",
            input_text="Completed On Worker",
            created_at=now - timedelta(minutes=2),
            finished_at=now,
        )
    )
    db_session.add(
        Job(
            id=jid_cancel,
            user_id=user.id,
            chat_id=user.id,
            status=JobStatus.CANCELLED.value,
            job_type=JobType.RECAP.value,
            engine="recap",
            input_text="Cancelled Cross Process",
            error_message="Cancelled by admin",
            created_at=now - timedelta(minutes=2),
            finished_at=now,
        )
    )
    await db_session.commit()

    api_mod.RECAP_JOBS_REGISTRY[jid_ok] = {
        "id": jid_ok,
        "user_id": user.id,
        "status": "QUEUED",
        "progress_percent": 0,
        "progress_step": "0/4",
        "created_at": time.time() - 120,
    }
    api_mod.RECAP_JOBS_REGISTRY[jid_cancel] = {
        "id": jid_cancel,
        "user_id": user.id,
        "status": "QUEUED",
        "progress_percent": 0,
        "progress_step": "0/4",
        "created_at": time.time() - 120,
    }

    try:
        live = await api_mod.get_live_jobs(user=user, db=db_session)
        assert live["active_count"] == 0
        by_id = {j["id"]: j for j in live["jobs"]}
        assert by_id[jid_ok]["status"] == JobStatus.SUCCESS.value
        assert by_id[jid_ok]["progress_percent"] == 100
        assert by_id[jid_cancel]["status"] == JobStatus.CANCELLED.value
        assert by_id[jid_cancel]["queue_position"] is None

        # Reset RAM stale again to test get_recap_status reconciliation directly.
        api_mod.RECAP_JOBS_REGISTRY[jid_ok]["status"] = "QUEUED"
        api_mod.RECAP_JOBS_REGISTRY[jid_cancel]["status"] = "QUEUED"

        st_ok = await api_mod.get_recap_status(jid_ok, user=user, db=db_session)
        st_cancel = await api_mod.get_recap_status(jid_cancel, user=user, db=db_session)
        assert st_ok["status"] == JobStatus.SUCCESS.value
        assert st_cancel["status"] == JobStatus.CANCELLED.value
        assert await api_mod._recap_active_job_count(db_session, user.id) == 0
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(jid_ok, None)
        api_mod.RECAP_JOBS_REGISTRY.pop(jid_cancel, None)


@pytest.mark.asyncio
async def test_round6_item5_legitimate_queued_job_stays_active_and_cancellable(
    db_session, monkeypatch
):
    """A job legitimately waiting for RECAP_EXECUTION_SEMAPHORE stays QUEUED,
    counts as 1 active job, and IS cancellable — then drops to 0 active."""
    admin, _ = await get_or_create_user(db_session, telegram_id=996004)
    monkeypatch.setattr(settings, "admin_ids_raw", str(admin.id))
    monkeypatch.setattr(settings, "recap_on_queue", True)

    resp = await api_mod.create_recap_job(
        _recap_req(title="Semaphore Queued Job"), user=admin, db=db_session, _gate=admin
    )
    jid = resp["recap_id"]
    try:
        live_before = await api_mod.get_live_jobs(user=admin, db=db_session)
        assert live_before["active_count"] == 1
        assert live_before["jobs"][0]["status"] == JobStatus.QUEUED.value

        cancel_res = await api_mod.cancel_recap_job(jid, user=admin, db=db_session)
        assert cancel_res["success"] is True
        assert cancel_res["status"] == "CANCELLED"

        live_after = await api_mod.get_live_jobs(user=admin, db=db_session)
        assert live_after["active_count"] == 0
        assert live_after["jobs"][0]["status"] == JobStatus.CANCELLED.value
        assert await api_mod._recap_active_job_count(db_session, admin.id) == 0
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)


def test_round6_item5_client_step4_cta_never_blocked_by_terminal_job():
    """Verify via Node that recapJobActive() returns False for a terminal job
    (even before the detail poll timer clears) and True for an active one."""
    start = APP_JS.index("function recapJobActive()")
    end = APP_JS.index("window.recapJobActive = recapJobActive;")
    fn_src = APP_JS[start:end]

    script = (
        fn_src
        + """
const LIVE_JOB_TERMINAL_STATUSES = ["SUCCESS", "FAILED", "CANCELLED"];
function liveJobsIsTerminal(s) { return LIVE_JOB_TERMINAL_STATUSES.includes(String(s||"").toUpperCase()); }
let state = { recapJobId: "rc_1" };
let _jobs = new Map([["rc_1", { id: "rc_1", kind: "recap", status: "QUEUED" }]]);
let LiveJobs = {
    jobs: _jobs,
    activeJobs: () => [..._jobs.values()].filter(j => !liveJobsIsTerminal(j.status)),
};
function recapJobIsActive() { return true; } // poll timer still open
const whileQueued = recapJobActive();
_jobs.set("rc_1", { id: "rc_1", kind: "recap", status: "FAILED" });
const afterFailed = recapJobActive();
_jobs.set("rc_1", { id: "rc_1", kind: "recap", status: "CANCELLED" });
const afterCancelled = recapJobActive();
_jobs.set("rc_1", { id: "rc_1", kind: "recap", status: "SUCCESS" });
const afterSuccess = recapJobActive();
console.log(JSON.stringify({ whileQueued, afterFailed, afterCancelled, afterSuccess }));
"""
    )
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    out = json.loads(proc.stdout.strip())
    assert out["whileQueued"] is True
    assert out["afterFailed"] is False
    assert out["afterCancelled"] is False
    assert out["afterSuccess"] is False

    # Detail poller terminal branches + cancel helper also sync LiveJobs & CTAs.
    poll_src = APP_JS[APP_JS.index("function startRecapJobPolling"):APP_JS.index("async function cancelRecapJobById")]
    assert 'LiveJobs.track({ id: jobId, kind: "recap", status: "FAILED"' in poll_src
    assert 'LiveJobs.track({ id: jobId, kind: "recap", status: "CANCELLED"' in poll_src
    assert 'LiveJobs.track({ id: jobId, kind: "recap", status: "SUCCESS"' in poll_src
    cancel_src = APP_JS[APP_JS.index("async function cancelRecapJobById"):APP_JS.index("window.cancelRecapJob")]
    assert "if (res.status === 409)" in cancel_src
    assert "LiveJobs.fetchNow()" in cancel_src
