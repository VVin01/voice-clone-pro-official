"""Toast + error-surface fixes (branch fix-toast-start-2026-10-31), webapp side.

Pins for:
- B1: a single client-side error mapper (mapErrorToToast) — known error
  families map to safe i18n copy, everything else falls to the flow's
  generic message, and the RAW error text never reaches a toast;
- B1: voice/recap FAILED toasts route through the mapper, recap-fail
  type is 'error' (was 'success'), 6000ms for the long guidance toasts;
- B2: toast hygiene — duplicate leading emojis stripped from i18n
  strings (the toast helper adds the type icon), duration essays and
  hardcoded Myanmar toasts moved to i18n keys, confirm-modal buttons
  follow the selected language, dedicated job-id-missing key;
- B5: admin server messages localized in api.py, hardcoded English
  admin toast prefixes replaced by i18n keys, native prompt() gone
  from the Mini App (slip rejection + copy-account use the premium
  input modal).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
API_PY = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")


def _locale_block(lang: str) -> str:
    m = re.search(rf"\n    {lang}: \{{(.*?)\n    \}}[,;]?\n", I18N_JS, re.S)
    assert m, f"locale block {lang} not found"
    return m.group(1)


def _keys(lang: str) -> set[str]:
    return set(re.findall(r"^\s{8}(\w+):", _locale_block(lang), re.M))


def _value(lang: str, key: str) -> str:
    m = re.search(rf'^\s{{8}}{key}: "((?:[^"\\]|\\.)*)"', _locale_block(lang), re.M)
    assert m, f"{lang}.{key} not found"
    return m.group(1)


NEW_KEYS = [
    "err_generic", "err_banned", "err_too_large", "err_voice_failed",
    "err_engine_busy", "err_recap_failed", "err_upload_failed",
    "err_send_telegram", "err_download_failed", "toast_job_id_missing",
    "toast_split_auto", "toast_duration_over", "toast_recap_over_limit",
    "toast_recap_over_hint_vip", "toast_recap_over_hint_free",
    "toast_scrub_hint", "toast_douyin_blocked", "toast_slip_no_file",
    "confirm_cancel", "confirm_ok", "account_number_title", "btn_copy",
    "admin_reject_title", "admin_reject_btn", "admin_reject_default",
    "admin_settings_saved", "admin_cancel_err", "admin_retry_err",
    "admin_delete_err", "admin_purge_err", "admin_purged",
    "admin_settings_save_err", "admin_gemini_saved",
    "admin_gemini_save_err", "admin_generic_err", "admin_done",
]


# ── i18n parity for the new keys ───────────────────────────────────────

def test_new_i18n_keys_exist_in_both_locales():
    my, en = _keys("my"), _keys("en")
    for key in NEW_KEYS:
        assert key in my, f"missing my.{key}"
        assert key in en, f"missing en.{key}"


# ── B2/D1: no duplicated type emoji at the start of toast strings ─────

def test_stripped_toast_strings_have_no_leading_type_emoji():
    # key -> emoji its toast type adds (success ✅ / warning ⚠️ / info ℹ️)
    cases = {
        "gate_verified_toast": "✅",
        "tutorial_coming_soon": "🚧",
        "voice_fallback_notice": "⚠️",
        "recap_fallback_note": "⚠️",
        "toast_script_saved": "💾",
        "toast_script_ready": "✅",
        "toast_script_warnings": "⚠️",
        "toast_split_auto": "✂️",
        "toast_scrub_hint": "💡",
        "toast_douyin_blocked": "⚠️",
    }
    for lang in ("my", "en"):
        for key, emoji in cases.items():
            assert not _value(lang, key).startswith(emoji), f"{lang}.{key} still starts with {emoji}"


TYPE_ICONS = {"success": "✅", "error": "❌", "warning": "⚠️", "info": "ℹ️"}


def test_no_toast_key_value_starts_with_a_type_icon():
    # Sweep every key actually rendered through showStudioToast with a
    # literal type: its value must not start with the icon that type
    # prepends, or it would render doubled. (Inline-HTML strings like
    # payment_notice are not toasts and keep their leading icons.)
    sites = re.findall(r"showStudioToast\(\s*t\(\"(\w+)\"\)\s*,\s*'(\w+)'", APP_JS)
    assert len(sites) > 20, "sweep found suspiciously few toast sites"
    for lang in ("my", "en"):
        for key, typ in sites:
            icon = TYPE_ICONS.get(typ)
            if icon:
                assert not _value(lang, key).startswith(icon), f"{lang}.{key} doubles the {typ} icon"
    # mapper results are always rendered as 'error' toasts
    for lang in ("my", "en"):
        for key in ("err_generic", "err_banned", "err_too_large", "err_voice_failed",
                    "err_engine_busy", "err_recap_failed", "err_upload_failed",
                    "err_send_telegram", "err_download_failed"):
            assert not _value(lang, key).startswith("❌"), f"{lang}.{key} doubles the error icon"


# ── B1: mapper exists and is the single route for failures ────────────

def test_mapper_and_helpers_defined_and_exported():
    assert "function mapErrorToToast(raw, flow" in APP_JS
    assert "function showErrorToast(err, flow" in APP_JS
    assert "window.mapErrorToToast = mapErrorToToast" in APP_JS
    assert "window.showErrorToast = showErrorToast" in APP_JS
    # mapper never returns the raw input — with ONE sanctioned
    # exception (E2, pre-deploy mini-fix 2026-10-31): the two
    # marker-gated pass-through families return our own closed-set
    # localized Burmese details unchanged. Exactly two such returns
    # exist, each behind a flow + marker-phrase gate; behaviour tests
    # in test_predeploy_minifix_2026_10_31.py prove raw/technical
    # strings still never pass.
    body = APP_JS.split("function mapErrorToToast")[1].split("function showErrorToast")[0]
    assert body.count("return rawStr;") == 2
    assert "if (flow === 'recap' && has(" in body
    assert "if (flow === 'voice' && has(" in body
    assert "return msg" not in body


def test_failed_job_toasts_route_through_mapper():
    assert "mapErrorToToast(data.error_message, 'voice')" in APP_JS
    assert "mapErrorToToast(errMsg, 'recap'), 'error', 6000" in APP_JS
    # the old raw-interpolating recap failure template is gone
    assert "${errMsg}" not in APP_JS
    assert "Movie Recap ဖန်တီးမှု အဆင်မပြေဖြစ်သွားပါသည်:" not in APP_JS
    # the recap FAILED toast uses 'error' styling around the mapper call
    idx = APP_JS.index("mapErrorToToast(errMsg, 'recap')")
    assert ", 'error', 6000)" in APP_JS[idx:idx + 80]


def test_submit_and_io_catches_route_through_mapper():
    for call in (
        "showErrorToast(err, 'voice')",
        "showErrorToast(err, 'recap')",
        "showErrorToast(err, 'download')",
        "showErrorToast(err, 'send')",
        "showErrorToast(err, 'generic')",
        "showErrorToast(e, 'generic')",
    ):
        assert call in APP_JS, call
    # Upload failures now surface in the persistent inline error box (with
    # retry) instead of a toast, but the shown message still routes through
    # the same mapper with the 'upload' context — never raw server text
    # (upload-ux pass, branch fix-upload-ux-2026-10-31).
    assert re.search(r"mapErrorToToast\(.*?,\s*\"upload\"\)", APP_JS, re.S)
    # no catch anywhere still toasts a raw .message directly
    assert not re.search(r"showStudioToast\((?:err|e)\.message", APP_JS)


def test_long_guidance_toasts_get_6000ms():
    assert 'showStudioToast(t("toast_douyin_blocked"), \'warning\', 6000)' in APP_JS
    assert "mapErrorToToast(errMsg, 'recap'), 'error', 6000" in APP_JS
    over = re.search(r'tf\("toast_recap_over_limit".*?\'warning\', 6000\)', APP_JS, re.S)
    assert over, "over-limit toast must pass 6000ms"


# ── B2: essays / hardcoded toasts moved to i18n ───────────────────────

def test_duration_essays_replaced_by_single_key():
    assert APP_JS.count('showStudioToast(t("toast_duration_over"), \'warning\')') == 2
    assert "သတိပေးချက်: တင်သွင်းထားသော" not in APP_JS
    assert "သတိပေးချက်: သင်ထည့်သွင်းထားသော" not in APP_JS


def test_hardcoded_toasts_moved_to_i18n():
    assert 'showStudioToast(t("toast_split_auto"), \'info\')' in APP_JS
    assert 'showStudioToast(t("toast_scrub_hint"), \'info\')' in APP_JS
    assert 'showStudioToast(t("toast_slip_no_file"), \'info\')' in APP_JS
    assert "ငွေလွှဲပြေစာ ပုံကို ရွေးချယ်ပေးပါခင်ဗျာ" not in APP_JS


def test_job_id_missing_uses_dedicated_key():
    assert APP_JS.count('showStudioToast(t("toast_job_id_missing"), \'warning\')') == 2


def test_confirm_modal_buttons_follow_language():
    assert 't("confirm_cancel")' in APP_JS
    assert 't("confirm_ok")' in APP_JS
    assert "မဟုတ်ဘူး / Cancel" not in APP_JS
    assert "သဘောတူ / OK" not in APP_JS


# ── B5: prompt() eradication + premium input modal ────────────────────

def test_no_native_prompt_anywhere_in_app_js():
    assert "prompt(" not in APP_JS


def test_premium_input_modal_exists():
    assert "function showStudioInputModal" in APP_JS
    assert "window.showStudioInputModal = showStudioInputModal" in APP_JS


def test_slip_reject_uses_modal_same_payload():
    body = APP_JS.split("async function adminRejectSlip")[1].split("\n}")[0]
    assert "showStudioInputModal" in body
    assert '"/api/admin/reject-slip"' in body
    assert "request_id: reqId" in body and "reason: reason" in body
    assert 'initialValue: t("admin_reject_default")' in body


def test_copy_account_uses_modal_not_prompt():
    body = APP_JS.split("function copyAccount")[1].split("\n}")[0]
    assert "showStudioInputModal" in body
    assert 'confirmLabel: t("btn_copy")' in body


# ── B5: admin toast prefixes localized ────────────────────────────────

def test_admin_hardcoded_english_prefixes_gone():
    for stale in (
        '"Cancel error: "', '"Retry failed: "', '"Retry error: "',
        '"Delete failed: "', '"Delete error: "', '"Purge failed: "',
        '"Purge error: "', '"Failed to save settings: "',
        '"Failed to save Gemini models: "',
        "Gemini Model Chain အောင်မြင်စွာ",
    ):
        assert stale not in APP_JS, stale
    for call in (
        'adminErrorText("admin_cancel_err"',
        'adminErrorText("admin_retry_err"',
        'adminErrorText("admin_delete_err"',
        'adminErrorText("admin_purge_err"',
        'adminErrorText("admin_settings_save_err"',
        'adminErrorText("admin_gemini_save_err"',
    ):
        assert call in APP_JS, call
    # purge success no longer hand-prepends ✅ (the helper adds it)
    assert "`✅ ${data.message" not in APP_JS
    assert 'showStudioToast(t("admin_gemini_saved"), \'success\')' in APP_JS


# ── B1/B5: api.py server-side wording ─────────────────────────────────

def test_api_upload_error_no_longer_leaks_exception():
    assert "Failed to save uploaded file: {exc}" not in API_PY
    assert "ဖိုင် သိမ်းဆည်းရာတွင် အဆင်မပြေ ဖြစ်သွားပါသည်" in API_PY
    assert "ဖိုင် အရွယ်အစား ကန့်သတ်ချက် ကျော်လွန်နေပါသည်" in API_PY


def test_api_url_block_detail_user_safe():
    assert "This URL is not allowed" not in API_PY
    assert API_PY.count('detail="ဒီလင့်ခ်ကို အသုံးပြု၍ မရပါခင်ဗျာ"') == 3
    # coded gate details are untouched (client gate logic depends on them)
    for code in ("USER_BANNED:", "GATE_NOT_CONFIGURED:", "MEMBERSHIP_REQUIRED:"):
        assert code in API_PY


def test_api_admin_messages_localized():
    for new in (
        "ကို Ban လိုက်ပါပြီ",
        "၏ Ban ကို ဖယ်ရှားပြီးပါပြီ",
        "ကို တန်းစီထဲ ပြန်ထည့်ပြီးပါပြီ",
        "ကို ပယ်ဖျက်ပြီးပါပြီ",
        '"message": "ဆက်တင်များ အောင်မြင်စွာ သိမ်းပြီးပါပြီ"',
        "ဈေးနှုန်း ပြင်ပြီးပါပြီ:",
        "ငွေပေးချေမှု အတည်ပြုပြီးပါပြီ",
        "ကို ငြင်းပယ်ပြီးပါပြီ",
        "ကို ပြင်ဆင်ပြီးပါပြီ",
        "Broadcast ပို့ရန် တန်းစီပြီးပါပြီ",
    ):
        assert new in API_PY, new
    for stale in (
        "re-queued successfully!", "marked as CANCELLED.",
        "Settings updated successfully!", "Price updated: ",
        "Approved payment for User", "Rejected payment request",
        "Broadcast queued for",
    ):
        assert stale not in API_PY, stale


# ── B1: mapper behaviour, executed in node against the real i18n ─────

def _run_mapper():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    start = APP_JS.index("function mapErrorToToast")
    end = APP_JS.index("function showStudioInputModal")
    mapper_src = APP_JS[start:end]
    driver = r"""
const out = {};
function run(lang) {
  currentLang = lang;
  const cases = {
    banned: mapErrorToToast("USER_BANNED: repeated abuse", "voice"),
    gate: mapErrorToToast("MEMBERSHIP_REQUIRED: join first", "recap"),
    gate2: mapErrorToToast("GATE_NOT_CONFIGURED", "voice"),
    toolarge: mapErrorToToast("413 Payload Too Large", "upload"),
    quota_voice: mapErrorToToast("ZeroGPU quota exceeded (402)", "voice"),
    quota_recap: mapErrorToToast("GPU duration quota exhausted", "recap"),
    link_recap: mapErrorToToast("ERROR: [youtube] abc: Sign in to confirm you're not a bot RAWSECRET123", "recap"),
    link_dl: mapErrorToToast("yt-dlp extract failed RAWSECRET123", "download"),
    unknown_voice: mapErrorToToast("weird failure RAWSECRET123", "voice"),
    unknown_recap: mapErrorToToast("weird failure RAWSECRET123", "recap"),
    unknown_dl: mapErrorToToast("weird failure RAWSECRET123", "download"),
    unknown_send: mapErrorToToast("weird failure RAWSECRET123", "send"),
    unknown_upload: mapErrorToToast("weird failure RAWSECRET123", "upload"),
    unknown_generic: mapErrorToToast("weird failure RAWSECRET123", "generic"),
    empty: mapErrorToToast("", "voice"),
    nul: mapErrorToToast(null, "recap"),
    err_obj: mapErrorToToast(new Error("USER_BANNED: x"), "voice"),
  };
  return cases;
}
out.my = run("my");
out.en = run("en");
console.log(JSON.stringify(out));
"""
    script = (
        "var window = {};\n"
        "var localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };\n"
        + I18N_JS + "\n" + mapper_src + "\n" + driver
    )
    tmp = ROOT / "tests/unit/_mapper_harness_tmp.js"
    try:
        tmp.write_text(script, encoding="utf-8")
        res = subprocess.run([node, str(tmp)], capture_output=True, text=True, timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_mapper_families_and_no_raw_leak():
    out = _run_mapper()
    for lang in ("my", "en"):
        r = out[lang]
        # raw marker must never survive into any mapped output
        for name, val in r.items():
            assert "RAWSECRET123" not in val, f"{lang}.{name} leaked raw text"
            assert val.strip(), f"{lang}.{name} empty"
    # family routing: banned/gate/too-large resolve to dedicated copy,
    # distinct from the generic fallback, in both languages
    my, en = out["my"], out["en"]
    assert my["banned"] != my["unknown_generic"]
    assert my["gate"] == my["gate2"]
    assert my["toolarge"] != my["unknown_generic"]
    assert my["quota_voice"] != my["unknown_voice"], "quota family must differ from voice default"
    # Journey audit 2026-10-31: a BLOCKED source (here the YouTube
    # sign-in wall) no longer shares the recap default — it maps to
    # the specific blocked-link copy (source named + the working
    # alternative). The no-raw-leak invariant above is unchanged.
    assert my["link_recap"] != my["unknown_recap"], "blocked link must differ from recap default"
    assert "YouTube" in my["link_recap"]
    assert my["link_dl"] == my["unknown_dl"]
    assert my["empty"] == my["unknown_voice"]
    assert my["nul"] == my["unknown_recap"]
    assert my["err_obj"] == my["banned"]
    assert en["banned"] != en["unknown_generic"]
