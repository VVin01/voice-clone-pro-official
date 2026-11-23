"""
Tests for History Failed Jobs, Recap Retry, and Toast Terminal State
Brief: History — Show failed jobs + Recap retry + Toast terminal state (2026-11-17)
"""

import asyncio
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.models.schema import AccountCategory, Job, JobStatus, JobType, User
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parent.parent.parent


def _run(coro):
    return asyncio.run(coro)


def _extract_fn(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.index(marker)
    i = src.index("{", start)
    depth = 0
    in_str = None
    in_regex = False
    in_line_comment = False
    in_block_comment = False
    escaped = False
    while i < len(src):
        ch = src[i]
        nxt = src[i + 1] if i + 1 < len(src) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
        elif in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 1
        elif in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
        elif in_regex:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "/":
                in_regex = False
        else:
            if ch == "/" and nxt == "/":
                in_line_comment = True
                i += 1
            elif ch == "/" and nxt == "*":
                in_block_comment = True
                i += 1
            elif ch in ('"', "'", "`"):
                in_str = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return src[start : i + 1]
        i += 1
    raise AssertionError(f"Unbalanced braces for function {name}")


def _run_node(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"Node script failed (rc={proc.returncode}):\n{proc.stderr}\nScript:\n{script}")
    try:
        return json.loads(proc.stdout)
    except Exception:
        return {"stdout": proc.stdout}


# ── 1. Asset Metrics and Budget Headroom ──────────────────────────────────────


def test_asset_metrics_and_budget_headroom():
    css_path = ROOT / "app/webapp/static/css/style.css"
    app_path = ROOT / "app/webapp/static/js/app.js"
    i18n_path = ROOT / "app/webapp/static/js/i18n.js"

    # Normalized LF byte measurement
    css_bytes = len(css_path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8"))
    app_bytes = len(app_path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8"))
    i18n_bytes = len(i18n_path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8"))
    total_lf_bytes = css_bytes + app_bytes + i18n_bytes

    approved_cap = 2_500_000
    assert total_lf_bytes <= approved_cap, (
        f"Total LF asset bytes {total_lf_bytes} exceeds cap {approved_cap}"
    )

    # Verify cap enforcement in the budget guard test file
    phase1_text = (ROOT / "tests/unit/test_phase_1_v2_liquid_glass.py").read_text(encoding="utf-8")
    assert "MAX_ASSET_BYTES_ALLOWED = 2_500_000" in phase1_text


# ── 2. Symmetric i18n Parity and Required Keys ───────────────────────────────


def test_i18n_symmetric_parity_and_keys():
    en_msg_file = ROOT / "locales/en/messages.json"
    my_msg_file = ROOT / "locales/my/messages.json"
    with open(en_msg_file, encoding="utf-8") as f:
        en_msg = json.load(f)
    with open(my_msg_file, encoding="utf-8") as f:
        my_msg = json.load(f)

    assert set(en_msg.keys()) == set(my_msg.keys()), "messages.json keys must be symmetric"
    assert "recap_retry_confirm_cost" in en_msg
    assert "recap_retry_confirm_admin" in en_msg

    # Check i18n.js parity via Node VM
    script = """
global.localStorage = { getItem: () => null, setItem: () => {} };
global.window = {};
const fs = require('fs');
const content = fs.readFileSync('app/webapp/static/js/i18n.js', 'utf8');
const fn = new Function(content + '; return translations;');
const tr = fn();
const myKeys = Object.keys(tr.my);
const enKeys = Object.keys(tr.en);
const diff1 = myKeys.filter(k => !(k in tr.en));
const diff2 = enKeys.filter(k => !(k in tr.my));
console.log(JSON.stringify({
    myCount: myKeys.length,
    enCount: enKeys.length,
    diff1,
    diff2,
    btn_retry_job_my: tr.my.btn_retry_job,
    btn_retry_job_en: tr.en.btn_retry_job,
    cost_confirm_my: tr.my.recap_retry_confirm_cost,
    cost_confirm_en: tr.en.recap_retry_confirm_cost,
    admin_confirm_my: tr.my.recap_retry_confirm_admin,
    admin_confirm_en: tr.en.recap_retry_confirm_admin
}));
"""
    res = _run_node(script)
    assert res["myCount"] == res["enCount"], f"i18n.js key counts differ: {res['myCount']} vs {res['enCount']}"
    assert res["diff1"] == [] and res["diff2"] == [], f"i18n.js asymmetric keys: {res['diff1']} {res['diff2']}"
    assert res["btn_retry_job_my"] == "🔄 ပြန် run"
    assert res["btn_retry_job_en"] == "🔄 Retry"
    assert "{credits}" in res["cost_confirm_my"]
    assert "{credits}" in res["cost_confirm_en"]
    assert "Admin" in res["admin_confirm_my"]
    assert "Admin" in res["admin_confirm_en"]


# ── 3. History Serializer: Failed Jobs Included, Active Excluded ───────────────


@pytest.mark.asyncio
async def test_history_serializer_failed_jobs_included_and_in_progress_excluded(db_session, tmp_path):
    user = User(
        id=770001,
        first_name="FailedHistUser",
        account_category=AccountCategory.PURCHASED.value,
        recap_credits=10,
        language_code="my",
    )
    db_session.add(user)
    await db_session.commit()

    now = datetime.now(timezone.utc)

    # 1. SUCCESS recap job with valid files (video + ai_thumbnail > 500b)
    recap_dir = settings.storage_root / "recap_output" / "rc_succ_1"
    recap_dir.mkdir(parents=True, exist_ok=True)
    video_file = recap_dir / "final_recap.mp4"
    video_file.write_bytes(b"mock_video_bytes" * 100)
    thumb_file = recap_dir / "ai_thumbnail.jpg"
    thumb_file.write_bytes(b"mock_thumb_bytes" * 100)

    j_success = Job(
        id="rc_succ_1",
        user_id=user.id,
        status=JobStatus.SUCCESS.value,
        job_type=JobType.RECAP.value,
        input_text="Success movie recap",
        engine="recap",
        result_path=str(video_file),
        created_at=now - timedelta(hours=1),
    )

    # 2. FAILED recap job
    j_recap_failed = Job(
        id="rc_fail_1",
        user_id=user.id,
        status=JobStatus.FAILED.value,
        job_type=JobType.RECAP.value,
        input_text="Failed movie recap",
        engine="recap",
        error_message="Downloader error: video 404",
        created_at=now - timedelta(hours=2),
    )

    # 3. FAILED voice job
    j_voice_failed = Job(
        id="vc_fail_1",
        user_id=user.id,
        status=JobStatus.FAILED.value,
        job_type=JobType.VOICE_CLONE.value,
        input_text="Failed voice job",
        engine="voxcpm",
        error_message="Reference audio too noisy",
        created_at=now - timedelta(hours=3),
    )

    # 4. QUEUED recap job (must NOT appear)
    j_recap_queued = Job(
        id="rc_queue_1",
        user_id=user.id,
        status=JobStatus.QUEUED.value,
        job_type=JobType.RECAP.value,
        input_text="Queued recap",
        engine="recap",
        created_at=now - timedelta(minutes=10),
    )

    # 5. RUNNING voice job (must NOT appear)
    j_voice_running = Job(
        id="vc_run_1",
        user_id=user.id,
        status=JobStatus.RUNNING.value,
        job_type=JobType.VOICE_CLONE.value,
        input_text="Running voice",
        engine="voxcpm",
        created_at=now - timedelta(minutes=5),
    )

    db_session.add_all([j_success, j_recap_failed, j_voice_failed, j_recap_queued, j_voice_running])
    await db_session.commit()

    payload = await api_mod.get_user_history(user=user, db=db_session)
    assert payload["success"] is True
    rows = {r["id"]: r for r in payload["history"]}

    # QUEUED and RUNNING must be excluded
    assert "rc_queue_1" not in rows
    assert "vc_run_1" not in rows

    # SUCCESS recap must be included and have deliverables
    assert "rc_succ_1" in rows
    s_row = rows["rc_succ_1"]
    assert s_row["status"] == "SUCCESS"
    assert s_row["download_url"] is not None
    assert s_row["deliverables"] is not None
    # Part E (2026-11-23): AI thumbnail removed; video/audio/srt/script deliverables remain.
    assert "video" in s_row["deliverables"]
    assert "audio" in s_row["deliverables"]
    assert "srt" in s_row["deliverables"]
    assert "script" in s_row["deliverables"]
    assert "thumbnail" not in s_row["deliverables"]

    # FAILED recap must be included with error_message and NO downloads
    assert "rc_fail_1" in rows
    rf_row = rows["rc_fail_1"]
    assert rf_row["status"] == "FAILED"
    assert rf_row["error_message"] == "Downloader error: video 404"
    assert rf_row["download_url"] is None
    assert rf_row["deliverables"] == {} or rf_row["deliverables"] is None

    # FAILED voice must be included with error_message and NO downloads
    assert "vc_fail_1" in rows
    vf_row = rows["vc_fail_1"]
    assert vf_row["status"] == "FAILED"
    assert vf_row["error_message"] == "Reference audio too noisy"
    assert vf_row["download_url"] is None
    assert vf_row["deliverables"] == {} or vf_row["deliverables"] is None


# ── 4. Recap Retry: Authorization, Concurrency Lock, and Source Availability ──


@pytest.mark.asyncio
async def test_recap_retry_double_tap_concurrency_lock():
    # Verify that in-flight set rejects duplicate retry
    job_id = "rc_test_inflight_1"
    async with api_mod._RECAP_RETRY_LOCK:
        api_mod._RECAP_RETRY_IN_FLIGHT.add(job_id)

    try:
        user = User(id=880001, first_name="LockUser", account_category=AccountCategory.PURCHASED.value)
        db = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await api_mod.retry_recap_job(job_id, user=user, db=db, _gate=user)
        assert exc_info.value.status_code == 409
        assert "already in flight" in exc_info.value.detail
    finally:
        async with api_mod._RECAP_RETRY_LOCK:
            api_mod._RECAP_RETRY_IN_FLIGHT.discard(job_id)


@pytest.mark.asyncio
async def test_recap_retry_validation_and_source_expiry(db_session):
    user = User(id=880002, first_name="ValUser", account_category=AccountCategory.PURCHASED.value, recap_credits=10)
    db_session.add(user)
    await db_session.commit()

    # 1. Job does not exist -> 404
    with pytest.raises(HTTPException) as exc_info:
        await api_mod.retry_recap_job("rc_missing", user=user, db=db_session, _gate=user)
    assert exc_info.value.status_code == 404

    # 2. Job is SUCCESS -> 409
    j_success = Job(
        id="rc_retry_succ",
        user_id=user.id,
        status=JobStatus.SUCCESS.value,
        job_type=JobType.RECAP.value,
        input_text="http://example.com/video.mp4",
        engine="recap",
    )
    db_session.add(j_success)
    await db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await api_mod.retry_recap_job("rc_retry_succ", user=user, db=db_session, _gate=user)
    assert exc_info.value.status_code == 409
    assert "မအောင်မြင်တဲ့ Recap" in exc_info.value.detail

    # 3. Failed job whose upload source is gone -> 409 honest upload missing
    fake_missing_upload = "temp_upload://non_existent_file_xyz.mp4"
    j_upload_expired = Job(
        id="rc_retry_expired_upload",
        user_id=user.id,
        status=JobStatus.FAILED.value,
        job_type=JobType.RECAP.value,
        input_text=fake_missing_upload,
        engine="recap",
    )
    db_session.add(j_upload_expired)
    await db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await api_mod.retry_recap_job("rc_retry_expired_upload", user=user, db=db_session, _gate=user)
    assert exc_info.value.status_code == 409
    assert "မူရင်း ဗီဒီယိုဖိုင်ကို မတွေ့တော့ပါ" in exc_info.value.detail


# ── 5. Toast Terminal State & DOM Rendering via Node VM ────────────────────────


def test_toast_terminal_state_and_history_dom_via_node():
    app_js_text = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")

    fn_set_failed = _extract_fn(app_js_text, "setLiveJobsFailedToast")
    fn_clear_failed = _extract_fn(app_js_text, "clearLiveJobsFailedToast")
    fn_render_pill = _extract_fn(app_js_text, "renderLiveJobsPill")
    fn_retention_badge = _extract_fn(app_js_text, "computeRetentionBadgeHtml")

    # Pass small script to Node, loading i18n from disk via require('fs')
    script = f"""
const vm = require('vm');
const fs = require('fs');

const elements = {{}};
function createElement(id) {{
    elements[id] = {{
        id,
        classList: {{
            _classes: new Set(),
            add(c) {{ this._classes.add(c); }},
            remove(c) {{ this._classes.delete(c); }},
            contains(c) {{ return this._classes.has(c); }}
        }},
        style: {{}},
        innerHTML: '',
        innerText: '',
        removeAttribute: () => {{}},
        setAttribute: () => {{}}
    }};
    return elements[id];
}}
const document = {{
    getElementById: (id) => elements[id] || createElement(id),
    querySelector: () => null,
    querySelectorAll: () => []
}};
const window = {{
    document,
    localStorage: {{ getItem: () => 'my', setItem: () => {{}} }}
}};

const ctx = {{
    window,
    document,
    localStorage: window.localStorage,
    escapeHtml: (s) => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
    LiveJobs: {{ activeJobs: () => [] }},
    liveJobsPillDismissedSig: null,
    liveJobsFailedPillJob: null,
    liveJobsFailedPillTimer: null,
    setTimeout: (fn, delay) => {{ return 123; }},
    clearTimeout: () => {{}},
    computeHistoryExpiryState: (j) => ({{ expired: false, expiresSoon: false, remainingHours: 24 }}),
}};

const i18nSrc = fs.readFileSync('app/webapp/static/js/i18n.js', 'utf8');
vm.runInNewContext(i18nSrc + '; globalThis.__tr = translations;', ctx);
ctx.t = (k) => (ctx.__tr.my && ctx.__tr.my[k]) || k;
ctx.tf = (k, p={{}}) => ctx.t(k);

vm.runInNewContext({json.dumps(fn_set_failed)}, ctx);
vm.runInNewContext({json.dumps(fn_clear_failed)}, ctx);
vm.runInNewContext({json.dumps(fn_render_pill)}, ctx);
vm.runInNewContext({json.dumps(fn_retention_badge)}, ctx);

// 1. Verify setLiveJobsFailedToast sets failure state on pill
ctx.setLiveJobsFailedToast({{ id: 'rc_failed_123', input_snippet: 'Breaking Bad Recap', status: 'FAILED' }});
const pill = document.getElementById('live-jobs-pill');
const stage = document.getElementById('live-jobs-pill-stage');
const isFailed = pill.classList.contains('is-failed');
const stageText = stage.innerText;

// 2. Clear failure toast
ctx.clearLiveJobsFailedToast();
const clearedFailed = pill.classList.contains('is-failed');

// 3. Retention badge returns empty string for non-SUCCESS
const failBadge = ctx.computeRetentionBadgeHtml({{ id: 'rc_1', status: 'FAILED' }});
const queuedBadge = ctx.computeRetentionBadgeHtml({{ id: 'rc_2', status: 'QUEUED' }});
const successBadge = ctx.computeRetentionBadgeHtml({{ id: 'rc_3', status: 'SUCCESS' }});

console.log(JSON.stringify({{
    isFailed,
    stageText,
    clearedFailed,
    failBadge,
    queuedBadge,
    hasSuccessBadge: typeof successBadge === 'string'
}}));
"""
    res = _run_node(script)
    assert res["isFailed"] is True, "Pill must have .is-failed class on failed toast"
    assert "Breaking Bad Recap" in res["stageText"], "Pill stage must contain job title"
    assert res["clearedFailed"] is False, "Pill must clear .is-failed after clearLiveJobsFailedToast"
    assert res["failBadge"] == "", "Failed job retention badge must be empty string"
    assert res["queuedBadge"] == "", "Queued job retention badge must be empty string"
    assert res["hasSuccessBadge"] is True, "Success job retention badge should produce badge string"
