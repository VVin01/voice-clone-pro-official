"""Pre-deploy mini-fix round (branch fix-toast-start-2026-10-31).

From deep-hunt #3's review of this branch's own diff, plus two small
hunt-#3 findings:

E2  The toast error mapper collapsed two kinds of OUR OWN localized
    details into flow generics: the segmented-recap shortfall message
    lost its exact top-up number, and pre-charge voice rejections
    (nothing was ever charged) were shown err_voice_failed's false
    "balance was refunded" claim. The mapper now passes the closed
    Burmese detail set through unchanged, matched on distinctive
    marker phrases; raw/technical strings still never pass.
E1  F4's server-side 24h initData rejection trapped long-open Mini
    App sessions behind the "Open via Telegram" modal. A 401 with a
    stale client auth_date now shows the dedicated session-expired
    message instead; other 401s keep the modal.
E8  SCRIPT_READY counts as active: the client tracks it in state (the
    poller stops at script-ready, but the job still owns the recap
    slot) and the server's active-count names it in the registry
    branch too (registry-only jobs; the DB branch already covered the
    normal path via the still-RUNNING row).
E4  The X1 preset_voice allowlist now lives in the queue service as
    well (ValueError), so no future caller can store a poisoned value;
    the webapp endpoints translate it to the same clean 400.
A1  payments.get_payment_account_info falls back per-field to the
    env-backed settings values when bot_settings rows are missing or
    empty, so bot invoices never show blank account numbers.
B1  Voice Telegram delivery gets the recap pipeline's 50 MB guard at
    all three send sites (worker auto-deliver, Mini App
    send-to-telegram, bot send_audio_ callback).
B2  Broadcast text over Telegram's 4096-char limit is rejected at
    both composition entry points (webapp endpoint + bot FSM) before
    any campaign/delivery exists, instead of silently skipping every
    recipient.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message, User as TgUser
from fastapi import HTTPException
from sqlalchemy import func, select

from app.config import settings
from app.models.enums import JobStatus, VoiceModelStatus, VoiceModelType
from app.models.schema import BotSetting, Job, VoiceModel
from app.services.ledger import get_or_create_user
from app.services.payments import get_payment_account_info
from app.services.queue import create_voice_clone_job
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
API_PY = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")

XSS_PAYLOAD = "x');alert(1);//"


async def _make_user(db_session, telegram_id, **fields):
    user, _ = await get_or_create_user(db_session, telegram_id=telegram_id)
    for key, value in fields.items():
        setattr(user, key, value)
    await db_session.commit()
    return user


def _run_node(script_body: str, tmp_name: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    tmp = ROOT / "tests/unit" / tmp_name
    try:
        tmp.write_text(script_body, encoding="utf-8")
        res = subprocess.run([node, str(tmp)], capture_output=True, text=True, timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


# ── E2: mapper pass-through families (node, real i18n) ────────────────

# The exact strings api.py produces (kept in step by the marker-drift
# test below, which asserts these phrases exist in the Python source).
SHORTFALL_WORKER = (
    "💳 Recap Credit မလုံလောက်ပါ — ဒီဗီဒီယိုရှည်ကို အပိုင်း 7 ပိုင်း "
    "ခွဲထုတ်ရမှာဖြစ်ပြီး အပိုင်းတစ်ခုစီ 1 Credit နှုန်းနဲ့ စုစုပေါင်း 7 Credits "
    "လိုအပ်ပါတယ်။ Credit ထပ်ဖြည့်ပြီးမှ ပြန်စမ်းပါခင်ဗျာ။"
)
SHORTFALL_SUBMIT = (
    "💳 ဒီဗီဒီယိုရှည်ကို အပိုင်း 4 ပိုင်း ခွဲထုတ်ရမှာဖြစ်ပြီး "
    "အပိုင်းတစ်ခုစီ 1 Credit နှုန်းနဲ့ စုစုပေါင်း 4 Credits လိုအပ်ပါတယ်။ "
    "လက်ကျန် Credit (2c) မလုံလောက်ပါ — Credit ထပ်ဖြည့်ပြီးမှ ပြန်စမ်းပါခင်ဗျာ။"
)
VOICE_INSUFFICIENT = "လက်ကျန်မိနစ် မလုံလောက်ပါ။"
VOICE_DAILY = "ယနေ့ အခမဲ့ ကန့်သတ်ချက် ပြည့်သွားပါပြီ။"
VOICE_CONCURRENT = "တစ်ပြိုင်တည်း လုပ်ဆောင်နေသော အလုပ်များနေပါသည်။ ခဏစောင့်ပြီးမှ ပြန်ကြိုးစားပါ။"


def _run_mapper_e2() -> dict:
    start = APP_JS.index("function mapErrorToToast")
    end = APP_JS.index("function showStudioInputModal")
    mapper_src = APP_JS[start:end]
    driver = r"""
const cases = {
  shortfall_worker: __SHORTFALL_WORKER__,
  shortfall_submit: __SHORTFALL_SUBMIT__,
  voice_insufficient: __VOICE_INSUFFICIENT__,
  voice_daily: __VOICE_DAILY__,
  voice_concurrent: __VOICE_CONCURRENT__,
};
const out = { my: {}, en: {} };
for (const lang of ["my", "en"]) {
  currentLang = lang;
  out[lang].shortfall_worker = mapErrorToToast(cases.shortfall_worker, "recap");
  out[lang].shortfall_submit = mapErrorToToast(cases.shortfall_submit, "recap");
  out[lang].shortfall_wrong_flow = mapErrorToToast(cases.shortfall_worker, "voice");
  out[lang].voice_insufficient = mapErrorToToast(cases.voice_insufficient, "voice");
  out[lang].voice_daily = mapErrorToToast(cases.voice_daily, "voice");
  out[lang].voice_concurrent = mapErrorToToast(cases.voice_concurrent, "voice");
  out[lang].voice_failed_generic = mapErrorToToast("weird failure", "voice");
  out[lang].recap_failed_generic = mapErrorToToast("weird failure", "recap");
  out[lang].raw_voice = mapErrorToToast("Insufficient balance: requested 847s, available 300s RAWSECRET123", "voice");
  out[lang].raw_recap = mapErrorToToast("ERROR: [youtube] abc: Sign in to confirm RAWSECRET123", "recap");
}
console.log(JSON.stringify(out));
"""
    for token, value in (
        ("__SHORTFALL_WORKER__", SHORTFALL_WORKER),
        ("__SHORTFALL_SUBMIT__", SHORTFALL_SUBMIT),
        ("__VOICE_INSUFFICIENT__", VOICE_INSUFFICIENT),
        ("__VOICE_DAILY__", VOICE_DAILY),
        ("__VOICE_CONCURRENT__", VOICE_CONCURRENT),
    ):
        driver = driver.replace(token, json.dumps(value, ensure_ascii=False))
    script = (
        "var window = {};\n"
        "var localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };\n"
        + I18N_JS + "\n" + mapper_src + "\n" + driver
    )
    return _run_node(script, "_minifix_mapper_tmp.js")


def test_e2_shortfall_message_survives_with_its_number():
    out = _run_mapper_e2()
    for lang in ("my", "en"):
        r = out[lang]
        # The localized shortfall copy passes through VERBATIM — the
        # exact part count the user must top up is preserved.
        assert r["shortfall_worker"] == SHORTFALL_WORKER
        assert "7 Credits" in r["shortfall_worker"]
        assert r["shortfall_submit"] == SHORTFALL_SUBMIT
        assert "4 Credits" in r["shortfall_submit"]
        # … and only in the recap flow.
        assert r["shortfall_wrong_flow"] != SHORTFALL_WORKER
        assert r["shortfall_wrong_flow"] == r["voice_failed_generic"]


def test_e2_precharge_voice_rejections_make_no_refund_claim():
    out = _run_mapper_e2()
    for lang in ("my", "en"):
        r = out[lang]
        assert r["voice_insufficient"] == VOICE_INSUFFICIENT
        assert r["voice_daily"] == VOICE_DAILY
        assert r["voice_concurrent"] == VOICE_CONCURRENT
        # The generic voice-failure copy claims a refund; the
        # pass-through texts must be different objects entirely.
        assert r["voice_insufficient"] != r["voice_failed_generic"]
    # In Burmese the false claim wording is "ပြန်အမ်း" (refunded):
    # none of the pre-charge texts may contain it.
    for key in ("voice_insufficient", "voice_daily", "voice_concurrent"):
        assert "ပြန်အမ်း" not in out["my"][key]


def test_e2_raw_strings_still_never_pass():
    out = _run_mapper_e2()
    for lang in ("my", "en"):
        r = out[lang]
        for name, val in r.items():
            assert "RAWSECRET123" not in val, f"{lang}.{name} leaked raw text"
        # An ENGLISH "insufficient balance" worker error is technical
        # text, not our localized detail: it maps to the generic.
        assert r["raw_voice"] == r["voice_failed_generic"]
        # A YouTube sign-in wall is a BLOCKED SOURCE (journey audit
        # 2026-10-31): it now maps to the specific blocked-link copy
        # (source named + the working alternative), NOT the recap
        # generic — and still never the raw string itself (the
        # RAWSECRET123 check above covers both languages).
        assert r["raw_recap"] != r["recap_failed_generic"]
        assert "YouTube" in r["raw_recap"]
    assert "Upload" in out["my"]["raw_recap"]


def test_e2_mapper_markers_match_api_py_source():
    # Drift guard: every marker phrase the JS mapper matches on must
    # exist in the Python strings it is meant to recognize.
    for marker in (
        "စုစုပေါင်း", "Credits လိုအပ်ပါတယ်",
        "လက်ကျန်မိနစ် မလုံလောက်ပါ",
        "ယနေ့ အခမဲ့ ကန့်သတ်ချက် ပြည့်သွားပါပြီ",
        "တစ်ပြိုင်တည်း လုပ်ဆောင်နေသော အလုပ်များနေပါသည်",
        "စာသား စာလုံးရေ ကန့်သတ်ချက်ထက် ကျော်လွန်နေပါသည်",
        "အသံမော်ဒယ် မတွေ့ရှိပါ",
        "အသံဖိုင် မတွေ့ရှိပါ",
        "ဒီအသံ မော်ဒယ် အဟောင်းကို မသုံးနိုင်တော့ပါ",
        "အလုပ်ဖန်တီးရာတွင် အမှားဖြစ်ပေါ်ပါသည်",
    ):
        assert marker in API_PY, marker
        # The JS needles sit in the lowercased matching space (the
        # mapper lowercases its input first; Burmese is caseless, so
        # only the Latin fragments differ in case).
        assert marker.lower() in APP_JS, marker


# ── E1: session-expired 401 handling (node, real i18n) ────────────────

def _auth_slice() -> str:
    start = APP_JS.index("let openViaTelegramShown")
    end_marker = "window.isAuthFailure = isAuthFailure;"
    end = APP_JS.index(end_marker) + len(end_marker)
    return APP_JS[start:end]


def _run_auth_harness(driver: str) -> dict:
    prelude = (
        "var window = {};\n"
        "var localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };\n"
        "var document = { getElementById: () => null };\n"
        "var toastCalls = [];\n"
        "function showStudioToast(msg, type, dur) { toastCalls.push({ msg: msg, type: type, dur: dur }); }\n"
        "var state = { tg: null };\n"
    )
    script = prelude + I18N_JS + "\n" + _auth_slice() + "\n" + driver
    return _run_node(script, "_minifix_auth_tmp.js")


def test_e1_stale_initdata_401_shows_session_expired_not_modal():
    driver = r"""
currentLang = "my";
const stale = Math.floor(Date.now() / 1000) - 25 * 3600;
state.tg = { initData: "", initDataUnsafe: { auth_date: stale } };
const out = {};
out.ret1 = isAuthFailure({ status: 401 });
out.toastsAfterFirst = toastCalls.length;
out.firstToast = toastCalls.length ? toastCalls[0].msg : null;
out.expected = t("err_session_expired");
out.modalShown = openViaTelegramShown;
out.ret2 = isAuthFailure({ status: 401 });
out.toastsAfterSecond = toastCalls.length;
// A fresh session for the same client still gets the modal path.
state.tg = { initData: "", initDataUnsafe: { auth_date: Math.floor(Date.now() / 1000) } };
out.ret3 = isAuthFailure({ status: 401 });
out.modalAfterFresh = openViaTelegramShown;
console.log(JSON.stringify(out));
"""
    out = _run_auth_harness(driver)
    assert out["ret1"] is True
    assert out["toastsAfterFirst"] == 1
    assert out["firstToast"] == out["expected"]
    assert "Session သက်တမ်း ကုန်သွားပါပြီ" in out["firstToast"]
    assert out["modalShown"] is False
    # Shown once only, even under a burst of polling 401s.
    assert out["ret2"] is True
    assert out["toastsAfterSecond"] == 1
    assert out["ret3"] is True
    assert out["modalAfterFresh"] is True


def test_e1_fresh_401_keeps_modal_and_non401_untouched():
    driver = r"""
currentLang = "en";
state.tg = { initData: "", initDataUnsafe: { auth_date: Math.floor(Date.now() / 1000) } };
const out = {};
out.ret500 = isAuthFailure({ status: 500 });
out.ret401 = isAuthFailure({ status: 401 });
out.modalShown = openViaTelegramShown;
out.toastCount = toastCalls.length;
console.log(JSON.stringify(out));
"""
    out = _run_auth_harness(driver)
    assert out["ret500"] is False
    assert out["ret401"] is True
    assert out["modalShown"] is True
    assert out["toastCount"] == 0


def test_e1_auth_date_parsed_from_raw_initdata_and_missing_is_not_expired():
    driver = r"""
currentLang = "my";
const stale = Math.floor(Date.now() / 1000) - 30 * 3600;
const out = {};
// No initDataUnsafe auth_date: fall back to parsing the raw string.
state.tg = { initData: "query_id=AAE&auth_date=" + stale + "&hash=abc", initDataUnsafe: {} };
out.expiredFromRaw = initDataExpired();
out.retStale = isAuthFailure({ status: 401 });
out.toastCount = toastCalls.length;
out.modalShown = openViaTelegramShown;
// No auth_date anywhere: NOT treated as expired (modal path).
state.tg = { initData: "query_id=AAE&hash=abc", initDataUnsafe: {} };
out.expiredMissing = initDataExpired();
console.log(JSON.stringify(out));
"""
    out = _run_auth_harness(driver)
    assert out["expiredFromRaw"] is True
    assert out["retStale"] is True
    assert out["toastCount"] == 1
    assert out["modalShown"] is False
    assert out["expiredMissing"] is False


def test_e1_session_expired_key_in_both_locales():
    def _value(lang: str, key: str) -> str:
        m = re.search(rf"\n    {lang}: \{{(.*?)\n    \}}[,;]?\n", I18N_JS, re.S)
        assert m, lang
        v = re.search(rf'^\s{{8}}{key}: "((?:[^"\\]|\\.)*)"', m.group(1), re.M)
        assert v, f"{lang}.{key}"
        return v.group(1)

    assert _value("my", "err_session_expired") == "Session သက်တမ်း ကုန်သွားပါပြီ — Mini App ကို ပိတ်ပြီး ပြန်ဖွင့်ပါ"
    assert _value("en", "err_session_expired") == "Session expired — please close and reopen the Mini App"
    # The expiry branch is consulted before the modal in isAuthFailure.
    body_start = APP_JS.index("function isAuthFailure(res)")
    body = APP_JS[body_start:body_start + 900]
    assert body.index("initDataExpired()") < body.index("showOpenViaTelegramModal()")


# ── E8: SCRIPT_READY counts as active ─────────────────────────────────

def test_e8_client_helper_counts_script_ready():
    start = APP_JS.index("function recapJobIsActive()")
    end = APP_JS.index("async function handleStartRecap()")
    helper_src = APP_JS[start:end]
    script = (
        "var state = { recapPollTimer: null, recapScriptReadyJobId: null };\n"
        + helper_src
        + "\nconst out = { idle: recapJobIsActive() };\n"
        + "state.recapScriptReadyJobId = 'rc_1';\n"
        + "out.scriptReady = recapJobIsActive();\n"
        + "state.recapScriptReadyJobId = null; state.recapPollTimer = 42;\n"
        + "out.polling = recapJobIsActive();\n"
        + "console.log(JSON.stringify(out));\n"
    )
    out = _run_node(script, "_minifix_e8_tmp.js")
    assert out == {"idle": False, "scriptReady": True, "polling": True}


def test_e8_client_tracks_and_clears_script_ready_state():
    # The poller's SCRIPT_READY branch records the job in state…
    branch = APP_JS.index('if (data.status === "SCRIPT_READY")')
    assert "state.recapScriptReadyJobId = jobId;" in APP_JS[branch:branch + 1200]
    # … and a successful render request clears it again.
    render = APP_JS.index("async function renderRecapScript(jobId)")
    assert "state.recapScriptReadyJobId = null;" in APP_JS[render:render + 1600]


@pytest.mark.asyncio
async def test_e8_server_count_includes_script_ready(db_session):
    user = await _make_user(db_session, 882001)
    registry = api_mod.RECAP_JOBS_REGISTRY
    saved = dict(registry)
    try:
        registry.clear()
        # Registry-only SCRIPT_READY job (DB insert failed at submit):
        # it must still occupy the user's slot.
        registry["rc_e8_a"] = {"user_id": user.id, "status": "SCRIPT_READY"}
        assert await api_mod._recap_active_job_count(db_session, user.id) == 1
        # A finished registry job does not count; another user's
        # script-ready job does not count either.
        registry["rc_e8_b"] = {"user_id": user.id, "status": "SUCCESS"}
        registry["rc_e8_c"] = {"user_id": user.id + 999, "status": "SCRIPT_READY"}
        assert await api_mod._recap_active_job_count(db_session, user.id) == 1
        # Normal path: DB row still RUNNING under a SCRIPT_READY
        # registry entry — union counts the job once, not twice.
        db_session.add(Job(
            id="rc_e8_a", user_id=user.id, status=JobStatus.RUNNING.value,
            job_type="RECAP", engine="recap",
        ))
        await db_session.commit()
        assert await api_mod._recap_active_job_count(db_session, user.id) == 1
    finally:
        registry.clear()
        registry.update(saved)


# ── E4: preset_voice invariant in the queue service ───────────────────

@pytest.mark.asyncio
async def test_e4_service_rejects_unknown_preset_voice(db_session):
    user = await _make_user(db_session, 882101)
    for bad in (XSS_PAYLOAD, "not-a-voice", ""):
        with pytest.raises(ValueError):
            await create_voice_clone_job(
                db_session, user.id, text="မင်္ဂလာပါ", preset_voice=bad
            )
    count = (await db_session.execute(select(func.count()).select_from(Job))).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_e4_service_accepts_curated_and_none(db_session):
    user = await _make_user(db_session, 882102)
    db_session.add(VoiceModel(
        name="VoxCPM Cloud", slug="voxcpm-cloud-ai",
        type=VoiceModelType.SYSTEM.value, owner_user_id=None,
        model_dir="cloud", pth_path="voxcpm",
        status=VoiceModelStatus.ACTIVE.value,
    ))
    await db_session.commit()
    job, reason = await create_voice_clone_job(
        db_session, user.id, text="မင်္ဂလာပါ", preset_voice="komin"
    )
    assert job is not None and reason == "QUEUED"
    assert job.preset_voice == "komin"
    job2, reason2 = await create_voice_clone_job(
        db_session, user.id, text="မင်္ဂလာပါ", preset_voice=None
    )
    # None is "no preset" and must not raise; it may still be refused
    # for ordinary queue reasons (concurrency), never with ValueError.
    assert reason2 in ("QUEUED", "CONCURRENT_JOB_LIMIT_EXCEEDED")
    if job2 is not None:
        assert job2.preset_voice is None


def test_e4_endpoints_translate_valueerror_to_same_400():
    # The shared detail constant backs the pre-check AND both
    # translation sites, so the user sees one identical 400 either way.
    assert API_PY.count("_INVALID_PRESET_VOICE_DETAIL") >= 4
    assert API_PY.count("except ValueError:") >= 2


# ── A1: payment account fallback ──────────────────────────────────────

def _set_env_accounts(monkeypatch):
    monkeypatch.setattr(settings, "payment_kpay_number", "ENV-KPAY-111")
    monkeypatch.setattr(settings, "payment_kpay_name", "Env KPay Name")
    monkeypatch.setattr(settings, "payment_wave_number", "ENV-WAVE-222")
    monkeypatch.setattr(settings, "payment_wave_name", "Env Wave Name")


@pytest.mark.asyncio
async def test_a1_db_rows_win_over_settings(db_session, monkeypatch):
    _set_env_accounts(monkeypatch)
    db_session.add_all([
        BotSetting(key="payment_kpay_number", value="DB-KPAY-999"),
        BotSetting(key="payment_kpay_name", value="DB KPay Name"),
        BotSetting(key="payment_wave_number", value="DB-WAVE-888"),
        BotSetting(key="payment_wave_name", value="DB Wave Name"),
    ])
    await db_session.commit()
    info = await get_payment_account_info(db_session)
    assert info == {
        "kpay_number": "DB-KPAY-999",
        "kpay_name": "DB KPay Name",
        "wave_number": "DB-WAVE-888",
        "wave_name": "DB Wave Name",
    }


@pytest.mark.asyncio
async def test_a1_missing_rows_fall_back_to_settings(db_session, monkeypatch):
    _set_env_accounts(monkeypatch)
    info = await get_payment_account_info(db_session)
    assert info == {
        "kpay_number": "ENV-KPAY-111",
        "kpay_name": "Env KPay Name",
        "wave_number": "ENV-WAVE-222",
        "wave_name": "Env Wave Name",
    }


@pytest.mark.asyncio
async def test_a1_partial_and_empty_rows_fall_back_per_field(db_session, monkeypatch):
    _set_env_accounts(monkeypatch)
    db_session.add_all([
        BotSetting(key="payment_kpay_number", value="DB-KPAY-999"),
        BotSetting(key="payment_wave_number", value=""),  # empty = unset
    ])
    await db_session.commit()
    info = await get_payment_account_info(db_session)
    assert info["kpay_number"] == "DB-KPAY-999"        # DB row wins
    assert info["wave_number"] == "ENV-WAVE-222"       # empty row → env
    assert info["kpay_name"] == "Env KPay Name"        # missing → env
    assert info["wave_name"] == "Env Wave Name"        # missing → env


# ── B1: voice Telegram delivery 50 MB guard ───────────────────────────

def _big_file(path: Path, size_mb: int = 51) -> Path:
    with open(path, "wb") as fh:
        fh.truncate(size_mb * 1024 * 1024)
    return path


class _FakeBotSession:
    async def close(self):
        pass


class _FakeBot:
    sent: list = []

    def __init__(self, token=None, **kwargs):
        self.session = _FakeBotSession()

    async def send_audio(self, **kwargs):
        _FakeBot.sent.append(kwargs)


@pytest.mark.asyncio
async def test_b1_api_send_to_telegram_rejects_oversize(db_session, tmp_path, monkeypatch):
    user = await _make_user(db_session, 882201)
    big = _big_file(tmp_path / "big_voice.wav")
    job = Job(
        id="vc_b1_big", user_id=user.id, status=JobStatus.SUCCESS.value,
        job_type="VOICE_CLONE", engine="voxcpm", result_path=str(big),
    )
    db_session.add(job)
    await db_session.commit()
    _FakeBot.sent = []
    monkeypatch.setattr("aiogram.Bot", _FakeBot)
    with pytest.raises(HTTPException) as exc:
        await api_mod.send_voice_to_telegram("vc_b1_big", user=user, db=db_session, _gate=user)
    assert exc.value.status_code == 413
    assert exc.value.detail == "file_too_large_telegram"
    assert _FakeBot.sent == []  # nothing was attempted


@pytest.mark.asyncio
async def test_b1_api_send_to_telegram_under_cap_still_sends(db_session, tmp_path, monkeypatch):
    user = await _make_user(db_session, 882202)
    small = tmp_path / "small_voice.wav"
    small.write_bytes(b"RIFF" + b"\x00" * 2048)
    job = Job(
        id="vc_b1_small", user_id=user.id, status=JobStatus.SUCCESS.value,
        job_type="VOICE_CLONE", engine="voxcpm", result_path=str(small),
    )
    db_session.add(job)
    await db_session.commit()
    _FakeBot.sent = []
    monkeypatch.setattr("aiogram.Bot", _FakeBot)
    resp = await api_mod.send_voice_to_telegram("vc_b1_small", user=user, db=db_session, _gate=user)
    assert resp["success"] is True
    assert len(_FakeBot.sent) == 1


def test_b1_client_handles_coded_too_large_detail():
    body_start = APP_JS.index("async function sendVoiceToTelegram")
    body = APP_JS[body_start:body_start + 2600]
    assert 'data.detail === "file_too_large_telegram"' in body
    assert 'showStudioToast(t("dl_too_large_telegram")' in body


@pytest.mark.asyncio
async def test_b1_worker_auto_deliver_oversize_notifies_instead(db_session, tmp_path, monkeypatch):
    """auto_deliver + >50 MB result: no doomed send_audio — the user
    gets the too-large notice with a Mini App button, job SUCCESS."""
    from app.models.schema import VoiceModel
    from app.services.ledger import get_minute_balance
    from app.worker import process_single_job

    monkeypatch.setattr(settings, "storage_root", tmp_path)

    user, _ = await get_or_create_user(db_session, telegram_id=882211)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 300

    pth_file = tmp_path / "test.pth"
    pth_file.write_bytes(b"pth_content")
    model = VoiceModel(
        name="Test Model", slug="test-model-b1", type="SYSTEM",
        model_dir=str(tmp_path), pth_path=str(pth_file),
        status="ACTIVE", is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    job, _ = await create_voice_clone_job(
        session=db_session, user_id=user.id, text="Long voice delivery test",
        edge_voice="my-MM-ThihaNeural", model_id=model.id, chat_id=882211,
    )
    assert job is not None
    job.auto_deliver = True  # the direct-delivery branch under test
    await db_session.commit()

    mock_bot = AsyncMock()

    def mock_voxcpm(text, output_wav, **kwargs):
        output_wav = Path(output_wav)
        output_wav.parent.mkdir(parents=True, exist_ok=True)
        with open(output_wav, "wb") as fh:
            fh.truncate(51 * 1024 * 1024)
        return output_wav

    with (
        patch("app.services.voxcpm_engine.VoxCPMEngine.synthesize", side_effect=mock_voxcpm),
        patch("app.worker.get_wav_duration_seconds", return_value=60),
        patch("app.worker.sync_output_to_cloud", new=AsyncMock()),
    ):
        processed = await process_single_job(
            session=db_session, job=job, bot=mock_bot, chat_id=882211, status_msg_id=777
        )
    assert processed is True
    await db_session.refresh(job)
    assert job.status == JobStatus.SUCCESS.value

    mock_bot.send_audio.assert_not_called()
    mock_bot.send_message.assert_awaited_once()
    kwargs = mock_bot.send_message.call_args[1]
    assert "50MB" in kwargs["text"]
    targets = [
        btn.web_app.url
        for row in kwargs["reply_markup"].inline_keyboard
        for btn in row if btn.web_app
    ]
    assert targets, "expected a Mini App button on the too-large notice"


@pytest.mark.asyncio
async def test_b1_bot_callback_oversize_routes_to_miniapp(db_session, tmp_path, monkeypatch):
    import app.handlers.clone as clone_mod
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    monkeypatch.setattr(settings, "storage_root", tmp_path)
    out_dir = tmp_path / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    big = _big_file(out_dir / "big_result.wav")

    user, _ = await get_or_create_user(db_session, 882221)
    job_id = "vc_b1_cb"
    db_session.add(Job(
        id=job_id, user_id=user.id, status=JobStatus.SUCCESS.value,
        job_type="VOICE_CLONE", engine="voxcpm", result_path=str(big),
    ))
    await db_session.commit()

    maker = async_sessionmaker(
        bind=db_session.bind, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(clone_mod, "AsyncSessionLocal", maker)

    from aiogram.types import CallbackQuery
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=882221, is_bot=False, first_name="Big Voice")
    cb.data = f"send_audio_{job_id}"
    cb.bot = MagicMock()
    cb.bot.send_audio = AsyncMock()
    cb.bot.send_message = AsyncMock()
    cb.answer = AsyncMock()

    await clone_mod.handle_send_audio_callback(cb)
    cb.bot.send_audio.assert_not_called()
    cb.bot.send_message.assert_awaited_once()
    kwargs = cb.bot.send_message.call_args[1]
    assert "50MB" in kwargs["text"]
    assert "/api/" not in kwargs["text"]
    targets = [
        btn.web_app.url
        for row in kwargs["reply_markup"].inline_keyboard
        for btn in row if btn.web_app
    ]
    assert targets


def test_b1_too_large_locale_key_in_both_bot_locales():
    from app.services.i18n import t
    for lang in ("my", "en"):
        text = t("clone_audio_too_large_text", lang, job_id="abc12345")
        assert "50MB" in text
        assert "abc12345" in text


# ── B2: broadcast 4096-char cap at both entry points ──────────────────

@pytest.mark.asyncio
async def test_b2_webapp_broadcast_over_limit_rejected(db_session):
    admin = await _make_user(db_session, 882301)
    req = api_mod.BroadcastRequest(message="x" * 4097)
    with pytest.raises(HTTPException) as exc:
        await api_mod.send_admin_broadcast(req, admin=admin, db=db_session)
    assert exc.value.status_code == 400
    assert "4096" in exc.value.detail


@pytest.mark.asyncio
async def test_b2_webapp_broadcast_at_limit_accepted(db_session, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "")
    admin = await _make_user(db_session, 882302)
    req = api_mod.BroadcastRequest(message="x" * 4096)
    resp = await api_mod.send_admin_broadcast(req, admin=admin, db=db_session)
    assert resp["success"] is True


class _FakeFSMContext:
    """In-memory FSMContext replica (same shape as test_broadcast.py)."""

    def __init__(self, initial_state=None, initial_data=None):
        self.current_state = initial_state
        self.data = initial_data or {}

    async def set_state(self, state):
        self.current_state = state

    async def get_state(self):
        return self.current_state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def clear(self):
        self.current_state = None
        self.data = {}


def _broadcast_msg(text: str):
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=999888, is_bot=False, first_name="ChiefAdmin")
    msg.text = text
    msg.answer = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_b2_bot_fsm_over_limit_rejected_before_campaign(db_session):
    from app.handlers.admin import AdminBroadcastFSM, handle_admin_broadcast_message_input

    settings.admin_ids_raw = "999888"
    await get_or_create_user(db_session, telegram_id=999888, language_code="en")
    state = _FakeFSMContext(
        initial_state=AdminBroadcastFSM.waiting_for_message,
        initial_data={"lang": "en"},
    )
    msg = _broadcast_msg("x" * 4097)
    await handle_admin_broadcast_message_input(msg, state, session=db_session)
    msg.answer.assert_awaited_once()
    assert "ရှည်လွန်းပါသည်" in msg.answer.call_args[0][0]
    # Still composing: no preview, no campaign state was stored.
    assert state.current_state == AdminBroadcastFSM.waiting_for_message
    assert "broadcast_text" not in state.data


@pytest.mark.asyncio
async def test_b2_bot_fsm_at_limit_reaches_preview(db_session):
    from app.handlers.admin import AdminBroadcastFSM, handle_admin_broadcast_message_input

    settings.admin_ids_raw = "999888"
    await get_or_create_user(db_session, telegram_id=999888, language_code="en")
    state = _FakeFSMContext(
        initial_state=AdminBroadcastFSM.waiting_for_message,
        initial_data={"lang": "en"},
    )
    msg = _broadcast_msg("x" * 4096)
    await handle_admin_broadcast_message_input(msg, state, session=db_session)
    assert state.current_state == AdminBroadcastFSM.confirming
    assert state.data["broadcast_text"] == "x" * 4096
