"""TikTok per-item IP-block — classification + History display fix.

Owner order 2026-11-02 ~09:42 with a History screenshot showing the
raw English extractor error on two rows:

    ERROR: [TikTok] 7391505677920816389: Your IP address is blocked
    from accessing this post

Live inventory behind it (admin APIs, 2026-11-02): the last 24h held
exactly 3 failed jobs — rc_1793505308_08cd6b (2026-11-01T03:55Z) and
rc_1793511624_d4c5a1 (2026-11-01T05:40Z), both the owner's, both this
same TikTok post with the IP-block string stored verbatim; plus
rc_1793545294_4bf9be (a FREE user's TikTok, deleted/private class —
already stored as clean classified Burmese). A third attempt on the
same post on 2026-10-31T11:53Z failed identically, while other TikTok
posts extracted fine from the same server minutes later — the block
is per-item, not a total IP block, and retrying the link is futile.

Root cause, two layers:
  * Server: classify_source_error() had no needle for this phrasing
    (its blocked branch required "403" + "forbidden"/"http error"),
    so get_info/download_* re-raised the raw yt-dlp error and the
    recap worker stored it verbatim on the job.
  * Client: blockedLinkInfo() already knew the phrasing (round-3/4),
    so toasts + the upload-tab auto-switch worked — but the admin
    queue/history cards render stored error_message raw, which is
    exactly the surface in the owner's screenshot.

Fixes pinned here:
  A. classify_source_error maps the IP-block family (exact phrasing +
     close variants) to the named-source blocked message with the
     upload-instead guidance — the same copy as the 403 branch.
  B. blockedLinkInfo recognises the server's own classified blocked
     sentence, so once the server stores clean text the recap poll
     keeps the named toast + auto-switch (no round-4 regression).
  C. displayJobError() is the single mapping for stored errors on the
     admin cards: classified copy passes through (prefix stripped,
     the cards add their own ⚠️), raw blocked-family errors map to
     the composed blocked message, everything else stays raw.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.downloader import MediaDownloader, classify_source_error

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
DOWNLOADER_PY = (ROOT / "app/services/downloader.py").read_text(encoding="utf-8")

TIKTOK_URL = "https://www.tiktok.com/@someone/video/7391505677920816389"
YOUTUBE_URL = "https://www.youtube.com/watch?v=abc123"

# The exact stored string from jobs rc_1793505308_08cd6b /
# rc_1793511624_d4c5a1 (owner screenshot, 2026-11-02).
RAW_IP_BLOCK = (
    "ERROR: [TikTok] 7391505677920816389: "
    "Your IP address is blocked from accessing this post"
)

# The exact sentence classify_source_error stores for a TikTok URL
# once fix A lands (403-branch copy, platform substituted).
SERVER_CLASSIFIED_TIKTOK = (
    "⚠️ TikTok က ကျွန်တော်တို့ ဆာဗာမှ ဗီဒီယို ဆွဲယူခြင်းကို ပိတ်ထားပါသည်။ "
    "ဗီဒီယိုဖိုင်ကို Mini App ရှိ '📁 Video Upload' မှ တိုက်ရိုက် တင်၍ "
    "ပြုလုပ်နိုင်ပါသည် ခင်ဗျာ။"
)


# ── A: server classification ────────────────────────────────────────

def test_classify_exact_screenshot_string():
    msg = classify_source_error(RAW_IP_BLOCK, TIKTOK_URL)
    assert msg == SERVER_CLASSIFIED_TIKTOK


def test_classify_ip_block_variants():
    variants = [
        "ERROR: [TikTok] 123: Your IP is blocked",
        "ERROR: [TikTok] 123: your ip address is blocked from accessing this post",
        "ERROR: [generic] blocked from accessing this post",
        "Unable to download webpage: IP address is blocked",
    ]
    for raw in variants:
        msg = classify_source_error(raw, TIKTOK_URL)
        assert msg is not None and msg.startswith("⚠️"), raw
        assert "TikTok" in msg and "ပိတ်ထားပါသည်" in msg, raw
        assert "Upload" in msg, raw  # upload-instead guidance


def test_classify_ip_block_names_other_platform():
    msg = classify_source_error(RAW_IP_BLOCK, YOUTUBE_URL)
    assert msg is not None and "YouTube" in msg and "ပိတ်ထားပါသည်" in msg


def test_classify_no_overreach():
    # Unrelated failures stay unclassified (callers keep old behaviour).
    assert classify_source_error("ERROR: some brand-new failure mode", TIKTOK_URL) is None
    # The deleted/private family keeps its own message, not the block one.
    msg = classify_source_error("ERROR: [TikTok] 1: No video formats found!", TIKTOK_URL)
    assert msg is not None and "ပိတ်ထားပါသည်" not in msg
    # The pre-existing 403 branch is untouched.
    msg = classify_source_error("HTTP Error 403: Forbidden", "https://vidmate.example.com/s/x")
    assert msg is not None and "ပိတ်ထားပါသည်" in msg


class _IpBlockYDL:
    """yt_dlp.YoutubeDL stand-in failing with the exact live error."""

    exc = Exception(RAW_IP_BLOCK)

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        raise self.exc

    def prepare_filename(self, info):
        return "unused.mp4"


def _patch_ydl(monkeypatch):
    import app.services.downloader as dl_mod

    monkeypatch.setattr(dl_mod, "unshorten_url", lambda u: u)
    monkeypatch.setattr(dl_mod.yt_dlp, "YoutubeDL", _IpBlockYDL)
    return dl_mod


def test_get_info_raises_classified_not_raw(monkeypatch, tmp_path):
    _patch_ydl(monkeypatch)
    dl = MediaDownloader(download_dir=str(tmp_path))
    with pytest.raises(RuntimeError) as ei:
        dl.get_info(TIKTOK_URL)
    assert str(ei.value) == SERVER_CLASSIFIED_TIKTOK
    assert "IP address is blocked" not in str(ei.value)


def test_download_video_raises_classified_not_raw(monkeypatch, tmp_path):
    _patch_ydl(monkeypatch)
    dl = MediaDownloader(download_dir=str(tmp_path))
    with pytest.raises(RuntimeError) as ei:
        dl.download_video(TIKTOK_URL, target_dir=str(tmp_path))
    assert str(ei.value) == SERVER_CLASSIFIED_TIKTOK


def test_downloader_source_carries_the_needles():
    for needle in ("ip address is blocked", "ip is blocked", "blocked from accessing"):
        assert needle in DOWNLOADER_PY


# ── B + C: client detector + stored-error display mapping ───────────

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


def _run_display_cases() -> dict:
    driver = r"""
const cases = {
  raw_ip_block: "ERROR: [TikTok] 7391505677920816389: Your IP address is blocked from accessing this post",
  server_classified: SERVER_CLASSIFIED,
  unrelated_raw: "'NoneType' object has no attribute 'setdefault'",
  vidmate_403_raw: "ERROR: [generic] share?id=0: Unable to download webpage: HTTP Error 403: Forbidden",
  empty: "",
};
const out = { my: {}, en: {} };
for (const lang of ["my", "en"]) {
  currentLang = lang;
  for (const [name, raw] of Object.entries(cases)) {
    out[lang][name + "_display"] = displayJobError(raw);
  }
  out[lang].classified_info = blockedLinkInfo(SERVER_CLASSIFIED);
  out[lang].classified_recap_toast = mapErrorToToast(SERVER_CLASSIFIED, "recap");
  out[lang].generic_recap = t("err_recap_failed");
}
console.log(JSON.stringify(out));
"""
    script = (
        "var window = {};\n"
        "var localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };\n"
        "const SERVER_CLASSIFIED = " + json.dumps(SERVER_CLASSIFIED_TIKTOK) + ";\n"
        + I18N_JS + "\n" + _mapper_slice() + "\n" + driver
    )
    return _run_node(script, "_tiktok_block_tmp.js")


def test_b_server_classified_copy_still_detected_blocked():
    out = _run_display_cases()
    for lang in ("my", "en"):
        assert out[lang]["classified_info"] == {"blocked": True, "source": "TikTok"}
        toast = out[lang]["classified_recap_toast"]
        assert "TikTok" in toast and toast != out[lang]["generic_recap"]


def test_c_display_raw_ip_block_maps_to_clean_burmese():
    out = _run_display_cases()
    for lang in ("my", "en"):
        disp = out[lang]["raw_ip_block_display"]
        assert "TikTok" in disp
        assert "IP address is blocked" not in disp
        assert "ERROR" not in disp


def test_c_display_classified_passthrough_strips_prefix():
    out = _run_display_cases()
    expected = SERVER_CLASSIFIED_TIKTOK.replace("⚠️ ", "", 1)
    for lang in ("my", "en"):
        assert out[lang]["server_classified_display"] == expected


def test_c_display_other_raw_errors_unchanged():
    out = _run_display_cases()
    for lang in ("my", "en"):
        assert out[lang]["unrelated_raw_display"] == "'NoneType' object has no attribute 'setdefault'"
        assert out[lang]["empty_display"] == ""
    # A raw 403 from a share site maps to the blocked message too.
    assert "IP address" not in out["my"]["vidmate_403_raw_display"]
    assert "403" not in out["my"]["vidmate_403_raw_display"]


def test_c_all_admin_error_sites_use_display_mapping():
    assert "escapeHtml(displayJobError(j.error_message))" in APP_JS       # admin queue card
    assert "const errText = displayJobError(j.error_message);" in APP_JS  # admin history card
    assert "displayJobError(data.error_message)" in APP_JS                # retry status box
    # No admin card interpolates a stored error raw any more.
    assert "escapeHtml(j.error_message)" not in APP_JS
    assert "escapeHtml(j.error_message.slice" not in APP_JS
    assert 'escapeHtml(data.error_message || "Execution failed")' not in APP_JS
