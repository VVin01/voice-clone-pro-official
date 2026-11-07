"""Batch AC pins (2026-11-04): Premium live UX.

Three pillars:
1. Live jobs layer — GET /api/jobs/live returns ALL the caller's
   non-terminal voice + recap jobs plus terminal jobs from the last
   ~10 minutes, in one bounded response; the global LiveJobs manager
   in app.js renders monotonically (never backwards), pauses while
   the document is hidden, catches up immediately on return, and
   renders a terminal transition exactly once.
2. No single-job lock-in / THE STUDIO SURVIVES SUBMIT — a persistent
   progress pill + jobs sheet (with working per-job Cancel: the
   existing recap endpoint, and a new owner-only voice cancel that
   mirrors the admin voice refund); submitting never tears the
   creation workspace down (no scroll-away, no control reset, Start
   free again at once), a parallel submit is explained IN PLACE
   (server concurrency is 1 active job per user for BOTH kinds —
   voice via queue.py, recap via create_recap_job — and is NOT
   weakened), and the detail view opens by choice with a Back path
   to the untouched studio.
3. Account-bound settings — GET/PUT /api/user/preferences on a
   startup-created user_preferences table; server wins over the
   localStorage cache; per-source blur-box geometry (blur_y/blur_h,
   blur_box_explicit) is NOT an account preference and is dropped.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.models.enums import JobStatus, JobType
from app.models.schema import Job, UserPreference
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
API_SRC = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
QUEUE_SRC = (ROOT / "app/services/queue.py").read_text(encoding="utf-8")

OWNER_TG = 990401
OTHER_TG = 990402

NEW_I18N_KEYS = [
    "live_jobs_count", "live_jobs_title", "live_jobs_empty",
    "live_job_queued", "live_job_queued_pos", "live_job_recent_done",
    "live_job_view_result", "live_job_elapsed", "live_prefs_saved",
    "live_prefs_saving", "live_submit_limit_title",
    "live_submit_limit_body", "voice_cancel_confirm",
    "voice_cancelled_toast", "studio_back_btn", "studio_inline_view",
    "confirm_title", "confirm_cost_voice", "confirm_cost_recap",
    "confirm_cost_recap_segmented", "confirm_cost_none",
    "confirm_watch_btn", "confirm_pill_hint", "submit_already_creating",
]

NOW = datetime.now(timezone.utc)


def _job(job_id, user_id, *, status="QUEUED", job_type="VOICE_CLONE",
         engine="voxcpm", text="hello", created=None, finished=None):
    return Job(
        id=job_id, user_id=user_id, status=status, job_type=job_type,
        engine=engine, input_text=text,
        created_at=created or NOW, finished_at=finished,
    )


# ── Pillar 3: preferences endpoints ────────────────────────────────────

@pytest.mark.asyncio
async def test_prefs_new_user_gets_todays_defaults(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    res = await api_mod.get_user_preferences(user=owner, db=db_session)
    assert res["success"] is True
    assert res["is_new"] is True
    prefs = res["preferences"]
    assert prefs == api_mod.DEFAULT_USER_PREFS
    assert prefs["recapSubFont"] == "padauk"
    assert prefs["voice_persona"] == "komin"
    assert prefs["recapBlurStrength"] == 18
    # A read writes nothing.
    assert await db_session.get(UserPreference, owner.id) is None


@pytest.mark.asyncio
async def test_prefs_round_trip_and_own_record_only(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    other, _ = await get_or_create_user(db_session, telegram_id=OTHER_TG)
    res = await api_mod.put_user_preferences(
        api_mod.UserPreferencesPayload(preferences={
            "recapSubFont": "noto_serif", "recapFontSize": 22,
            "voice_persona": "mathiri", "recapStoryStyle": "dramatic",
        }),
        user=owner, db=db_session,
    )
    assert res["preferences"]["recapSubFont"] == "noto_serif"
    assert res["preferences"]["recapFontSize"] == 22
    # Untouched fields still carry defaults (server-side merge).
    assert res["preferences"]["recapRatio"] == "9:16"

    got = await api_mod.get_user_preferences(user=owner, db=db_session)
    assert got["is_new"] is False
    assert got["preferences"]["voice_persona"] == "mathiri"
    assert got["preferences"]["recapStoryStyle"] == "dramatic"

    # The other user's record is untouched: defaults, still new.
    got_other = await api_mod.get_user_preferences(user=other, db=db_session)
    assert got_other["is_new"] is True
    assert got_other["preferences"] == api_mod.DEFAULT_USER_PREFS


@pytest.mark.asyncio
async def test_prefs_partial_update_merges(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    await api_mod.put_user_preferences(
        api_mod.UserPreferencesPayload(preferences={"recapFontSize": 20}),
        user=owner, db=db_session,
    )
    res = await api_mod.put_user_preferences(
        api_mod.UserPreferencesPayload(preferences={"recapLines": 2}),
        user=owner, db=db_session,
    )
    assert res["preferences"]["recapFontSize"] == 20
    assert res["preferences"]["recapLines"] == 2


def test_prefs_sanitizer_drops_blur_geometry_and_unknowns():
    clean = api_mod._sanitize_user_prefs({
        "recapBlurY": 40, "recapBlurH": 30, "blur_box_explicit": True,
        "recapBlurBoxTouched": True, "evil_key": "x",
        "recapFontSize": 24, "recapRatio": "16:9",
        # Batch AG: blur x/w are per-source like blur y/h — dropped;
        # sub x/w are account preferences like recapSubY — kept.
        "recapBlurX": 20, "recapBlurW": 60,
        "recapSubX": 65, "recapSubW": 70,
    })
    assert "recapBlurY" not in clean
    assert "recapBlurH" not in clean
    assert "recapBlurX" not in clean
    assert "recapBlurW" not in clean
    assert "blur_box_explicit" not in clean
    assert "recapBlurBoxTouched" not in clean
    assert "evil_key" not in clean
    assert clean["recapFontSize"] == 24
    assert clean["recapRatio"] == "16:9"
    assert clean["recapSubX"] == 65
    assert clean["recapSubW"] == 70


def test_prefs_sanitizer_validates_ranges_enums_and_slugs():
    clean = api_mod._sanitize_user_prefs({
        "recapFontSize": 99,               # out of range -> dropped
        "recapRatio": "4:3",               # not an enum -> dropped
        "recapZoom": 5.0,                  # out of range -> dropped
        "recapMirror": "yes",              # not a bool -> dropped
        "voice_persona": "not-a-voice",    # not curated -> dropped
        "recapVoice": "komin",             # curated -> kept
        "recapSubFont": "serif",           # legacy alias -> noto_serif
        "recapBlurStrength": 26,
    })
    assert "recapFontSize" not in clean
    assert "recapRatio" not in clean
    assert "recapZoom" not in clean
    assert "recapMirror" not in clean
    assert "voice_persona" not in clean
    assert clean["recapVoice"] == "komin"
    assert clean["recapSubFont"] == "noto_serif"
    assert clean["recapBlurStrength"] == 26


def test_prefs_server_wins_merge():
    merged = api_mod._effective_user_prefs({"recapFontSize": 28})
    assert merged["recapFontSize"] == 28
    assert merged["recapSubFont"] == api_mod.DEFAULT_USER_PREFS["recapSubFont"]
    # Every stored field is in the documented field list.
    for key in api_mod.DEFAULT_USER_PREFS:
        assert (
            key in api_mod.USER_PREF_ENUMS or key in api_mod.USER_PREF_BOOLS
            or key in api_mod.USER_PREF_INTS or key in api_mod.USER_PREF_STRINGS
            or key in api_mod.USER_PREF_FLOATS
        ), key


@pytest.mark.asyncio
async def test_prefs_put_drops_blur_geometry_from_storage(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    await api_mod.put_user_preferences(
        api_mod.UserPreferencesPayload(preferences={
            "recapBlurY": 12, "recapBlurH": 44, "recapFontSize": 18,
        }),
        user=owner, db=db_session,
    )
    row = await db_session.get(UserPreference, owner.id)
    stored = json.loads(row.prefs_json)
    assert "recapBlurY" not in stored and "recapBlurH" not in stored
    assert stored["recapFontSize"] == 18


# ── Pillar 1: bulk live-jobs endpoint ──────────────────────────────────

@pytest.mark.asyncio
async def test_live_jobs_own_only_shape_and_recent_terminal(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    other, _ = await get_or_create_user(db_session, telegram_id=OTHER_TG)
    db_session.add_all([
        _job("ac-v1", owner.id, status="QUEUED"),
        _job("ac-v2", owner.id, status="SUCCESS",
             finished=NOW - timedelta(minutes=5)),
        _job("ac-v3", owner.id, status="SUCCESS",
             created=NOW - timedelta(hours=2),
             finished=NOW - timedelta(minutes=55)),  # too old: excluded
        _job("ac-v4", owner.id, status="FAILED",
             finished=NOW - timedelta(minutes=9)),
        _job("ac-r1", owner.id, status="RUNNING", job_type="RECAP",
             engine="recap", text="Some Movie Title"),
        _job("ac-x1", other.id, status="RUNNING"),  # someone else's
    ])
    await db_session.commit()

    res = await api_mod.get_live_jobs(user=owner, db=db_session)
    assert res["success"] is True
    ids = {j["id"] for j in res["jobs"]}
    assert ids == {"ac-v1", "ac-v2", "ac-v4", "ac-r1"}
    assert res["active_count"] == 2
    by_id = {j["id"]: j for j in res["jobs"]}
    for j in res["jobs"]:
        assert set(j) >= {
            "id", "kind", "status", "stage_index", "stage_total",
            "progress_percent", "elapsed_seconds", "title",
        }
    assert by_id["ac-v1"]["kind"] == "voice"
    assert by_id["ac-v1"]["progress_percent"] == 45
    assert by_id["ac-v1"]["queue_position"] is not None
    assert by_id["ac-r1"]["kind"] == "recap"
    assert by_id["ac-r1"]["title"] == "Some Movie Title"
    assert by_id["ac-v2"]["progress_percent"] == 100


@pytest.mark.asyncio
async def test_live_jobs_recap_registry_overlay(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    db_session.add(_job("rc_ac_overlay", owner.id, status="RUNNING",
                        job_type="RECAP", engine="recap"))
    await db_session.commit()
    api_mod.RECAP_JOBS_REGISTRY["rc_ac_overlay"] = {
        "user_id": owner.id, "status": "RUNNING", "progress_percent": 62,
        "progress_step": "3/4", "progress_stage": "tts",
        "progress_label": "voice", "title": "Overlay Title",
        "created_at": time.time(),
    }
    try:
        res = await api_mod.get_live_jobs(user=owner, db=db_session)
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop("rc_ac_overlay", None)
    job = {j["id"]: j for j in res["jobs"]}["rc_ac_overlay"]
    assert job["progress_percent"] == 62
    assert job["stage_index"] == 3 and job["stage_total"] == 4
    assert job["progress_stage"] == "tts"


@pytest.mark.asyncio
async def test_live_jobs_registry_only_script_ready_counts_active(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    api_mod.RECAP_JOBS_REGISTRY["rc_ac_parked"] = {
        "user_id": owner.id, "status": "SCRIPT_READY",
        "progress_percent": 40, "progress_step": "2/4",
        "progress_stage": "script_ready", "progress_label": "",
        "title": "Parked", "created_at": time.time(),
    }
    try:
        res = await api_mod.get_live_jobs(user=owner, db=db_session)
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop("rc_ac_parked", None)
    ids = {j["id"] for j in res["jobs"]}
    assert "rc_ac_parked" in ids
    assert res["active_count"] == 1


def test_live_stage_from_step_parsing():
    assert api_mod._live_stage_from_step("3/4", "RUNNING") == (3, 4)
    assert api_mod._live_stage_from_step("", "SUCCESS") == (1, 1)
    assert api_mod._live_stage_from_step(None, "RUNNING") == (0, 4)
    assert api_mod.LIVE_JOBS_RECENT_TERMINAL_MINUTES == 10
    assert api_mod.LIVE_JOBS_LIMIT <= 50


# ── Pillar 2: voice cancel endpoint (new, owner-only) ─────────────────

@pytest.mark.asyncio
async def test_voice_cancel_owner_queued(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    db_session.add(_job("ac-cancel-1", owner.id, status="QUEUED"))
    await db_session.commit()
    res = await api_mod.cancel_voice_job("ac-cancel-1", user=owner, db=db_session)
    assert res["success"] is True and res["status"] == "CANCELLED"
    job = await db_session.get(Job, "ac-cancel-1")
    assert job.status == JobStatus.CANCELLED.value
    assert job.finished_at is not None


@pytest.mark.asyncio
async def test_voice_cancel_other_user_404_and_recap_404(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    other, _ = await get_or_create_user(db_session, telegram_id=OTHER_TG)
    db_session.add_all([
        _job("ac-cancel-2", owner.id, status="RUNNING"),
        _job("rc_ac_cancel", owner.id, status="RUNNING",
             job_type="RECAP", engine="recap"),
    ])
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await api_mod.cancel_voice_job("ac-cancel-2", user=other, db=db_session)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await api_mod.cancel_voice_job("rc_ac_cancel", user=owner, db=db_session)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_voice_cancel_terminal_409(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    db_session.add(_job("ac-cancel-3", owner.id, status="SUCCESS", finished=NOW))
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await api_mod.cancel_voice_job("ac-cancel-3", user=owner, db=db_session)
    assert exc.value.status_code == 409


def test_concurrency_guards_not_weakened():
    # Voice: the queue service still rejects a second active job.
    assert "CONCURRENT_JOB_LIMIT_EXCEEDED" in QUEUE_SRC
    assert "max_concurrent_jobs_per_user" in QUEUE_SRC
    # Recap: the submit path still rejects with 429 before charging.
    assert "A recap job is already running for your account" in API_SRC
    assert '"/jobs/live"' in API_SRC
    assert '"/jobs/{job_id}/cancel"' in API_SRC
    assert API_SRC.count('"/user/preferences"') >= 2


# ── Pillar 1/2/3: frontend source pins ─────────────────────────────────

def test_livejobs_manager_source_pins():
    assert 'fetch("/api/jobs/live"' in APP_JS
    assert "liveJobsMergeJob" in APP_JS
    assert "visibilitychange" in APP_JS
    assert 'addEventListener("focus"' in APP_JS
    assert "document.hidden" in APP_JS
    assert "LiveJobs.start()" in APP_JS


def test_pill_and_sheet_markup_and_wiring():
    assert 'id="live-jobs-pill"' in INDEX_HTML
    assert 'onclick="openLiveJobsSheet()"' in INDEX_HTML
    assert 'id="live-jobs-sheet"' in INDEX_HTML
    assert 'id="live-jobs-sheet-list"' in INDEX_HTML
    # Sheet Cancel wires the EXISTING recap flow and the voice endpoint.
    cancel_slice = APP_JS[APP_JS.index("window.liveJobCancel"):APP_JS.index("window.liveJobOpen")]
    assert "cancelRecapJobById(jobId)" in cancel_slice
    assert "/api/jobs/${jobId}/cancel" in cancel_slice
    # Recently-finished jobs get a jump-to-result action.
    assert "live_job_view_result" in APP_JS
    assert "openRecapInPlayer(jobId)" in APP_JS


def test_history_in_place_update_pins():
    assert "applyLiveJobToHistory" in APP_JS
    assert "hub-live-meta" in APP_JS
    assert "hubStatusBorder(job.status)" in APP_JS
    # Track actions open the detail BY CHOICE via the new openers.
    assert "openRecapDetail('${j.id}')" in APP_JS
    assert "openVoiceDetail('${j.id}')" in APP_JS


def test_studio_survives_submit_pins():
    voice_submit = APP_JS[APP_JS.index("async function handleVoiceGenerate()"):APP_JS.index("function startJobPolling")]
    recap_submit = APP_JS[APP_JS.index("async function handleStartRecap()"):APP_JS.index("function startRecapJobPolling")]
    # (a) No scroll-away / teardown on submit in either studio, and the
    # submit handlers never rewrite the workspace inputs.
    for body in (voice_submit, recap_submit):
        assert "scrollIntoView" not in body
    assert "textInput.value =" not in voice_submit
    assert "urlInput.value =" not in recap_submit
    # Slim inline status lines carry the job in the studio.
    assert 'showStudioInlineStatus("voice"' in voice_submit
    assert 'showStudioInlineStatus("recap"' in recap_submit
    assert 'id="voice-inline-status"' in INDEX_HTML
    assert 'id="recap-inline-status"' in INDEX_HTML
    # (b) A second, ADJUSTED submit is possible — through the
    # duplicate-submit guard (see the guard pins below): the button
    # re-arms when any setting/input changes. A parallel submit while
    # a job is active is explained in place instead of silently dropped.
    assert 'studioSubmitBegin("voice")' in voice_submit
    assert 'studioSubmitSuccess("voice")' in voice_submit
    assert 'studioSubmitBegin("recap")' in recap_submit
    assert 'studioSubmitSuccess("recap")' in recap_submit
    assert 'showLiveSubmitLimit("voice")' in voice_submit
    assert 'showLiveSubmitLimit("recap")' in recap_submit
    # The recap poller no longer locks Start or forces the detail open.
    poller = APP_JS[APP_JS.index("function startRecapJobPolling"):APP_JS.index("// ── Recap job actions")]
    assert "state.recapDetailOpen) progressBox.classList.remove" in poller
    assert "btn.disabled = true" not in poller
    # Completion no longer scroll-hijacks either studio's poller
    # (deep-link opens, which are by-choice, keep their scroll).
    voice_poller = APP_JS[APP_JS.index("function startJobPolling"):APP_JS.index("// ── Recap duration caps")]
    # The SUCCESS render itself must not scroll (the floating alert's
    # own button may still scroll when the user taps it — by choice).
    assert 'resultBox.classList.remove("hidden");\n                    resultBox.scrollIntoView' not in voice_poller
    assert "resultBox.scrollIntoView" not in poller
    assert "no scroll hijack on completion" in voice_poller
    assert "no scroll hijack on completion" in poller


def test_back_to_studio_pins():
    assert "window.backToStudio" in APP_JS
    assert "onclick=\"backToStudio('voice')\"" in INDEX_HTML
    assert "onclick=\"backToStudio('recap')\"" in INDEX_HTML
    back = APP_JS[APP_JS.index("window.backToStudio"):APP_JS.index("function showLiveSubmitLimit")]
    # The back path only closes the detail and returns to the tab —
    # it never touches a control value or a settings field.
    assert "recapRatio" not in back and "selectedPersona" not in back
    assert ".value =" not in back


def test_submit_confirmation_handoff_pins():
    card = INDEX_HTML[INDEX_HTML.index('id="submit-confirm"'):INDEX_HTML.index('id="live-jobs-sheet"')]
    # Exactly two primary actions: Watch the job / Back to studio.
    assert card.count('onclick="submitConfirm') == 2
    assert 'onclick="submitConfirmWatch()"' in card
    assert 'onclick="submitConfirmBack()"' in card
    # Success indication, plainly-stated cost slot, live snippet, and
    # the pill-teaching hint line.
    assert 'data-i18n="confirm_title"' in card
    assert 'id="submit-confirm-cost"' in card
    assert 'id="submit-confirm-stage"' in card
    assert 'id="submit-confirm-fill"' in card
    assert 'data-i18n="confirm_pill_hint"' in card
    # Both studios hand off to it with their real cost lines.
    voice_submit = APP_JS[APP_JS.index("async function handleVoiceGenerate()"):APP_JS.index("function startJobPolling")]
    recap_submit = APP_JS[APP_JS.index("async function handleStartRecap()"):APP_JS.index("function startRecapJobPolling")]
    assert 'showSubmitConfirmation("voice", data.job_id, t("confirm_cost_voice"))' in voice_submit
    assert 'showSubmitConfirmation("recap", data.recap_id, confirmCostText)' in recap_submit
    assert 'tf("confirm_cost_recap", { credits: data.credits_charged })' in recap_submit
    assert 'tf("confirm_cost_recap_segmented", { parts: data.estimated_parts })' in recap_submit
    # The snippet is fed by LiveJobs; Watch opens the detail by choice.
    assert "LiveJobs.subscribe((job) => {" in APP_JS
    assert "renderSubmitConfirmSnippet(job)" in APP_JS
    watch = APP_JS[APP_JS.index("window.submitConfirmWatch"):APP_JS.index("window.submitConfirmBack")]
    assert "openRecapDetail(job.id)" in watch and "openVoiceDetail(job.id)" in watch


def test_submit_guard_wiring_pins():
    assert "setupSubmitGuards()" in APP_JS
    assert "guardRearmCheck" in APP_JS
    assert 't("submit_already_creating")' in APP_JS
    # A failed submit releases the guard (no job was created).
    voice_submit = APP_JS[APP_JS.index("async function handleVoiceGenerate()"):APP_JS.index("function startJobPolling")]
    recap_submit = APP_JS[APP_JS.index("async function handleStartRecap()"):APP_JS.index("function startRecapJobPolling")]
    assert 'studioSubmitFailure("voice")' in voice_submit
    assert 'studioSubmitFailure("recap")' in recap_submit


def test_submit_guard_behaviour_node():
    start = APP_JS.index("function createSubmitGuard()")
    end = APP_JS.index("const voiceSubmitGuard")
    core = APP_JS[start:end]
    script = (
        core
        + "\nconst g = createSubmitGuard();\n"
        + "const first = guardBegin(g, 'fp-A');\n"
        # (a) in-flight double-tap: exactly one submit proceeds
        + "const doubleTap = guardBegin(g, 'fp-A');\n"
        + "guardSuccess(g);\n"
        # (b) unchanged configuration cannot resubmit
        + "const unchanged = guardBegin(g, 'fp-A');\n"
        # (c) changing any control re-arms the button
        + "const rearmed = guardRearmCheck(g, 'fp-B');\n"
        + "const afterChange = guardBegin(g, 'fp-B');\n"
        + "guardFailure(g);\n"
        + "const afterFailure = guardBegin(g, 'fp-B');\n"
        + "console.log(JSON.stringify({ first, doubleTap, unchanged, rearmed, afterChange, afterFailure }));\n"
    )
    out = _run_node(script)
    assert out["first"] == "ok"
    assert out["doubleTap"] == "in-flight"
    assert out["unchanged"] == "unchanged"
    assert out["rearmed"] is True
    assert out["afterChange"] == "ok"
    # A failed submit created no job, so the same config may retry.
    assert out["afterFailure"] == "ok"


def test_prefs_frontend_pins():
    assert 'fetch("/api/user/preferences"' in APP_JS          # GET on launch
    assert 'method: "PUT"' in APP_JS                            # debounced save
    assert "loadAccountPrefs()" in APP_JS
    assert "saveAccountPrefsSoon" in APP_JS
    assert "USER_PREFS_CACHE_KEY" in APP_JS or "vcp_user_prefs_cache" in APP_JS
    collect = APP_JS[APP_JS.index("function collectAccountPrefs()"):APP_JS.index("function cacheAccountPrefs")]
    # Per-source blur geometry is never collected for the account.
    assert "recapBlurY" not in collect and "recapBlurH" not in collect
    assert "blur_box_explicit" not in collect
    # Batch AG: blur x/w stay per-source (never collected); sub x/w
    # ARE collected, like recapSubY.
    assert "recapBlurX" not in collect and "recapBlurW" not in collect
    assert "recapSubX" in collect and "recapSubW" in collect
    assert 'id="prefs-saved-note"' in INDEX_HTML


def test_new_i18n_keys_in_both_languages():
    for key in NEW_I18N_KEYS:
        assert I18N_JS.count(key + ":") >= 2, key


def test_cache_bump_r62():
    assert INDEX_HTML.count("?v=20261104_r112") == 4


# ── LiveJobs manager: node behaviour harness ──────────────────────────

def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    tmp = ROOT / "tests/unit/_ac_harness_tmp.js"
    try:
        tmp.write_text(script, encoding="utf-8")
        res = subprocess.run([node, str(tmp)], capture_output=True, text=True, timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def _livejobs_slice() -> str:
    start = APP_JS.index("const LIVE_JOB_TERMINAL_STATUSES")
    end = APP_JS.index("window.LiveJobs = LiveJobs;") + len("window.LiveJobs = LiveJobs;")
    return APP_JS[start:end]


def test_livejobs_merge_monotonic_and_terminal_sticky_node():
    script = (
        "const window = {};\nconst document = { hidden:false, addEventListener(){} };\n"
        "function getAuthHeaders(){return {};}\n"
        "function renderLiveJobsPill(){}\nfunction renderLiveJobsSheet(){}\n"
        "function applyLiveJobToHistory(){}\n"
        + _livejobs_slice()
        + "\nconst a = liveJobsMergeJob(null, {id:'1', status:'RUNNING', progress_percent:40, stage_index:2, stage_total:4});\n"
        + "const b = liveJobsMergeJob(a, {id:'1', status:'RUNNING', progress_percent:15, stage_index:1, stage_total:4});\n"
        + "const c = liveJobsMergeJob(b, {id:'1', status:'SUCCESS', progress_percent:100, stage_index:4, stage_total:4});\n"
        + "const d = liveJobsMergeJob(c, {id:'1', status:'RUNNING', progress_percent:20, stage_index:1, stage_total:4});\n"
        + "console.log(JSON.stringify({b_pct:b.progress_percent, b_stage:b.stage_index, c_status:c.status, d_status:d.status, d_pct:d.progress_percent, term:liveJobsIsTerminal('SUCCESS'), act:liveJobsIsActive('SCRIPT_READY')}));\n"
    )
    out = _run_node(script)
    # The reconnect reset (40%/step2 -> 15%/step1) never renders.
    assert out["b_pct"] == 40 and out["b_stage"] == 2
    assert out["c_status"] == "SUCCESS"
    # Terminal is sticky.
    assert out["d_status"] == "SUCCESS" and out["d_pct"] == 100
    assert out["term"] is True and out["act"] is True


def test_livejobs_manager_hidden_pause_catchup_terminal_once_node():
    script = (
        "let fetchCalls = 0;\n"
        "let snapshot = [{id:'j1', kind:'voice', status:'RUNNING', progress_percent:50, stage_index:0, stage_total:1}];\n"
        "async function fetch(){ fetchCalls++; return { ok:true, json: async () => ({ jobs: snapshot }) }; }\n"
        "function getAuthHeaders(){return {};}\n"
        "function renderLiveJobsPill(){}\nfunction renderLiveJobsSheet(){}\n"
        "let historyCalls = [];\nfunction applyLiveJobToHistory(j){ historyCalls.push(j.id + ':' + j.status); }\n"
        "const _listeners = {};\n"
        "const document = { hidden:false, addEventListener:(n,f)=>{_listeners[n]=f;} };\n"
        "const window = { addEventListener:(n,f)=>{_listeners['w:'+n]=f;} };\n"
        + _livejobs_slice()
        + "\n(async () => {\n"
        + "  LiveJobs.start();\n"
        + "  await new Promise(r => setTimeout(r, 20));\n"
        + "  const afterStart = fetchCalls;\n"
        + "  document.hidden = true; LiveJobs.schedule();\n"
        + "  const pausedTimer = LiveJobs.timer === null;\n"
        + "  await new Promise(r => setTimeout(r, 30));\n"
        + "  const noFetchWhileHidden = fetchCalls === afterStart;\n"
        + "  snapshot = [{id:'j1', kind:'voice', status:'SUCCESS', progress_percent:100, stage_index:1, stage_total:1}];\n"
        + "  document.hidden = false; _listeners['visibilitychange']();\n"
        + "  await new Promise(r => setTimeout(r, 20));\n"
        + "  const catchUpFetched = fetchCalls > afterStart;\n"
        + "  await LiveJobs.fetchNow();\n"
        + "  const terminalOnce = historyCalls.filter(x => x === 'j1:SUCCESS').length;\n"
        + "  if (LiveJobs.timer) clearTimeout(LiveJobs.timer);\n"
        + "  console.log(JSON.stringify({ afterStart, pausedTimer, noFetchWhileHidden, catchUpFetched, terminalOnce, status: LiveJobs.jobs.get('j1').status }));\n"
        + "})().catch(e => { console.error(e); process.exit(1); });\n"
    )
    out = _run_node(script)
    assert out["afterStart"] >= 1
    assert out["pausedTimer"] is True
    assert out["noFetchWhileHidden"] is True
    assert out["catchUpFetched"] is True
    # Exactly one terminal render/notification for the transition.
    assert out["terminalOnce"] == 1
    assert out["status"] == "SUCCESS"


def test_back_to_studio_restores_workspace_node():
    start = APP_JS.index("state.voiceDetailOpen = false;")
    end = APP_JS.index("function showLiveSubmitLimit")
    section = APP_JS[start:end]
    script = (
        "const window = {};\n"
        "const state = { voiceDetailOpen:true, recapDetailOpen:true, recapRatio:'16:9', recapFontSize:22, selectedPersona:'thiha', activeJobId:null, recapJobId:null, pollTimer:1, recapPollTimer:1 };\n"
        "function _el(){ return { hidden:false, classList:{ _s:new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);}, contains(c){return this._s.has(c);} }, scrollIntoView(){} }; }\n"
        "const els = { 'voice-progress-box':_el(), 'recap-progress-box':_el(), 'tab-voice':_el(), 'tab-recap':_el() };\n"
        "const document = { getElementById:(id)=>els[id] || null };\n"
        "function startJobPolling(){}\nfunction startRecapJobPolling(){}\n"
        + section
        + "\nwindow.backToStudio('recap'); window.backToStudio('voice');\n"
        + "console.log(JSON.stringify({ recapRatio: state.recapRatio, recapFontSize: state.recapFontSize, selectedPersona: state.selectedPersona, recapBoxHidden: els['recap-progress-box'].classList.contains('hidden'), voiceBoxHidden: els['voice-progress-box'].classList.contains('hidden'), recapDetailOpen: state.recapDetailOpen }));\n"
    )
    out = _run_node(script)
    # (c) Returning from the detail view restores the workspace exactly:
    # every control value is untouched, only the detail box closes.
    assert out["recapRatio"] == "16:9"
    assert out["recapFontSize"] == 22
    assert out["selectedPersona"] == "thiha"
    assert out["recapBoxHidden"] is True and out["voiceBoxHidden"] is True
    assert out["recapDetailOpen"] is False
