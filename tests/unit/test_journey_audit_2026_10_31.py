"""User-journey audit fixes (branch journey-audit-2026-10-31).

Tonight's evidence: a brand-new FREE user burned 8 attempts on
TikTok/VidMate links those sites block, while the UI (a) never warned
that some sources refuse outside downloads, (b) answered failures
with "try again" generics, (c) presented the extract endpoint's
graceful fallback as verified metadata ("✅ Ready", fake 03:30), and
(d) told first-run users they had 0 free minutes (the monthly grant
only landed via the bot /start flow or at voice submit). The same
audit found the language toggle silently reverting (an un-awaited
persist POST racing the profile re-read).

Covered here:
J1  Blocked-source failures map to specific, actionable copy
    (source named + the flow's working alternative) in my+en, for
    both the recap and download flows; unknown errors still map to
    the flow generics and raw text still never passes through.
J2  /api/user/me grants the monthly free allowance (idempotently),
    so first-run balances are truthful everywhere the profile is
    rendered, and exposes completed_jobs for the welcome-strip gate.
J3  The extract endpoint marks fallback metadata metadata_verified
    =False (and verified payloads True) so the client never presents
    invented details as facts.
J4  Welcome strip + link help markup/i18n wiring is present, and
    every new i18n key exists in BOTH languages.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.models.enums import JobStatus, JobType
from app.models.schema import Job
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
API_PY = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")


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


def _mapper_slice() -> str:
    start = APP_JS.index("function mapErrorToToast")
    end = APP_JS.index("function showStudioInputModal")
    return APP_JS[start:end]


# ── J1: blocked-source mapper copy ────────────────────────────────────

def _run_blocked_mapper() -> dict:
    driver = r"""
const cases = {
  tiktok_noformats: "yt-dlp extract error: ERROR: No video formats found! (tiktok)",
  vidmate_403: "Download failed: HTTP Error 403: Forbidden (vidmate)",
  unsupported: "ERROR: Unsupported URL",
  youtube_wall: "ERROR: [youtube] abc123: Sign in to confirm you\u2019re not a bot",
  unknown: "weird failure",
  quota: "ZeroGPU quota exhausted",
};
const out = { my: {}, en: {} };
for (const lang of ["my", "en"]) {
  currentLang = lang;
  out[lang].tiktok_recap = mapErrorToToast(cases.tiktok_noformats, "recap");
  out[lang].tiktok_download = mapErrorToToast(cases.tiktok_noformats, "download");
  out[lang].vidmate_recap = mapErrorToToast(cases.vidmate_403, "recap");
  out[lang].unsupported_download = mapErrorToToast(cases.unsupported, "download");
  out[lang].youtube_recap = mapErrorToToast(cases.youtube_wall, "recap");
  out[lang].unknown_recap = mapErrorToToast(cases.unknown, "recap");
  out[lang].blocked_in_voice_flow = mapErrorToToast(cases.tiktok_noformats, "voice");
  out[lang].generic_recap = t("err_recap_failed");
  out[lang].generic_download = t("err_download_failed");
  out[lang].generic_voice = t("err_voice_failed");
  out[lang].info_tiktok = blockedLinkInfo(cases.tiktok_noformats);
  out[lang].info_unknown = blockedLinkInfo(cases.unknown);
  out[lang].info_quota = blockedLinkInfo(cases.quota);
}
console.log(JSON.stringify(out));
"""
    script = (
        "var window = {};\n"
        "var localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };\n"
        + I18N_JS + "\n" + _mapper_slice() + "\n" + driver
    )
    return _run_node(script, "_journey_mapper_tmp.js")


def test_j1_blocked_sources_get_specific_actionable_copy():
    out = _run_blocked_mapper()
    for lang in ("my", "en"):
        r = out[lang]
        # Source is NAMED for the user, in both flows.
        assert "TikTok" in r["tiktok_recap"]
        assert "TikTok" in r["tiktok_download"]
        assert "VidMate" in r["vidmate_recap"]
        assert "YouTube" in r["youtube_recap"]
        # …and it is NOT the old generic any more.
        assert r["tiktok_recap"] != r["generic_recap"]
        assert r["tiktok_download"] != r["generic_download"]
        # Unknown errors keep the flow generics; the blocked mapping
        # must not leak into the voice flow.
        assert r["unknown_recap"] == r["generic_recap"]
        assert r["blocked_in_voice_flow"] == r["generic_voice"]
        # The shared detector agrees with the mapper.
        assert r["info_tiktok"] == {"blocked": True, "source": "TikTok"}
        assert r["info_unknown"]["blocked"] is False
        assert r["info_quota"]["blocked"] is False
    # Flow-correct alternatives: recap points at direct upload (and
    # says no credit was lost); download points at the original link.
    assert "Upload" in out["my"]["tiktok_recap"]
    assert "Credit" in out["my"]["tiktok_recap"]
    assert "Upload" not in out["my"]["tiktok_download"]
    assert "upload the video file directly" in out["en"]["tiktok_recap"]


# ── J2: /api/user/me grants + completed_jobs ──────────────────────────

@pytest.mark.asyncio
async def test_j2_profile_grants_monthly_free_and_counts_completed(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=987654321)
    await db_session.commit()

    profile = await api_mod.get_user_profile(user=user, db=db_session)
    # A Mini-App-first user (no bot /start, no prior submit) sees the
    # configured monthly grant: 180s = 3.0 minutes, not a false zero.
    assert profile["balance_seconds"] == 180
    assert profile["balance_minutes"] == 3.0
    assert profile["completed_jobs"] == 0

    db_session.add(Job(
        user_id=user.id,
        job_type=JobType.VOICE_CLONE.value,
        status=JobStatus.SUCCESS.value,
    ))
    await db_session.commit()

    profile2 = await api_mod.get_user_profile(user=user, db=db_session)
    assert profile2["completed_jobs"] == 1
    # Grant is idempotent per month: the second read must NOT stack
    # another 180s on top.
    assert profile2["balance_seconds"] == 180


# ── J3: extract metadata honesty flag ─────────────────────────────────

def test_j3_extract_marks_unverified_fallback():
    assert '"metadata_verified": True' in API_PY
    assert '"metadata_verified": False' in API_PY
    # The client consumes the flag in the recap analyze success path.
    assert 'data.metadata_verified === false' in APP_JS
    assert 'recap_unverified_note' in APP_JS


# ── J4: welcome strip + link help wiring ──────────────────────────────

NEW_KEYS = (
    "err_link_blocked", "err_link_blocked_named",
    "err_link_advice_upload", "err_link_advice_dl",
    "link_help_recap", "link_help_dl",
    "recap_unverified_note",
    "welcome_strip_text", "welcome_strip_btn",
)


def test_j4_new_i18n_keys_exist_in_both_languages():
    driver = r"""
const keys = __KEYS__;
const out = {};
for (const k of keys) {
  out[k] = { my: typeof translations.my[k] === "string" && translations.my[k].length > 0,
             en: typeof translations.en[k] === "string" && translations.en[k].length > 0 };
}
console.log(JSON.stringify(out));
"""
    script = (
        "var window = {};\n"
        "var localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };\n"
        + I18N_JS + "\n" + driver.replace("__KEYS__", json.dumps(list(NEW_KEYS)))
    )
    out = _run_node(script, "_journey_i18n_tmp.js")
    for key in NEW_KEYS:
        assert out[key]["my"], f"{key} missing in my"
        assert out[key]["en"], f"{key} missing in en"


def test_j4_welcome_strip_and_help_markup_present():
    # Strip lives inside the Home pane, before the samples card, and
    # is hidden by default (JS gate decides).
    home = INDEX_HTML.split('id="tab-home"')[1].split('id="tab-voice"')[0]
    assert 'id="home-welcome-strip"' in home
    assert 'class="welcome-strip hidden"' in home
    assert 'onclick="dismissWelcomeStrip()"' in home
    assert "switchTab('tab-recap')" in home
    # Quiet inline help inside the two link-paste components.
    assert 'data-i18n="link_help_recap"' in INDEX_HTML
    assert 'data-i18n="link_help_dl"' in INDEX_HTML
    # The JS gate: Free-only, zero completed jobs, per-device dismiss.
    assert 'localStorage.getItem("vcp_welcome_dismissed")' in APP_JS
    assert "u.completed_jobs === 0" in APP_JS
    assert "refreshWelcomeStrip();" in APP_JS


def test_j4_language_toggle_persists_before_profile_reread():
    # The toggle must await the language persist POST before
    # loadUserProfile() re-reads (and adopts) the stored language.
    toggle = APP_JS[APP_JS.index("async function toggleLanguage"):]
    toggle = toggle[: toggle.index("loadCuratedVoices();")]
    assert "await window.__langPersistPromise" in toggle
    assert "setLanguage(nextLang);" in toggle
    assert "__langPersistPromise = fetch" in I18N_JS
