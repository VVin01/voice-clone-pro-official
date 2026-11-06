"""Batch BI — caption-detector false positives + user auto-blur
toggle + bot voice-name mismatch (2026-11-06).

BI-1: Milo's free recap of "Me at the zoo" (a caption-FREE, noisy
240p source) shipped with FOUR full-width blur strips across the
picture — the Batch AI detector's "bright glyph with dark outline"
row scorer fired on scene texture (face, fence rails, jacket) and
every spurious band shipped. The fix: (1) text-cluster scoring —
only bright-with-outline pixels in word-stroke-like horizontal runs
(1-14 px at the 270-px sample scale; the window's lower bound was
tuned from 2 to 1 on the genuine GlunZpix30I fixture, whose glyph
mass is mostly 1-px anti-aliased strokes) count toward a row; (2) a
prominence gate — a band's peak agg must clear 4x the median agg
across all rows of its own frame matrix; (3) a 2-band cap keeping
the strongest survivors. The real zoo source cannot be downloaded
in the sandbox (YouTube bot wall), so synthetic fixtures are the
proof, exactly as the Batch AI suite proved its cases.

Batch BM amendment (2026-11-06): guard (2), the prominence gate,
is REMOVED — the BL drift investigation proved it kills genuine
weak captions on busy real frames (the genuine GlunZpix30I strip:
peak/median ratio ~1.4x against the gate's 4x floor). The zoo
protection is now carried by (1), (3), and Batch BM's caption-zone
drop (a band ships only when its whole extent lies in the top
[0.00, 0.24] / bottom [0.58, 1.00] caption zones, applied BEFORE
the cap per owner Option A). The BI-1 pins below were re-pinned at
that level: same fixtures, same outcomes, different mechanism —
each re-pin says so in place.

Batch BQ amendment (owner decision 2026-11-06, same day): the
zone is now BOTTOM ONLY — the top zone is removed (both live
top-zone bands that day were scene texture). The cap pin below
was re-pinned again: its top band is now a zone-drop witness
and a third bottom band keeps the cap exercised.

BI-2: the user's own ON/OFF switch for auto-detect blur (owner
request 2026-11-06 14:31) — recap form switch (default ON, persisted
like every recap pref), a per-job boolean riding RecapConfigRequest
into BOTH detector call sites (preview + render); OFF skips the
detector entirely. A manual blur box stays authoritative/unaffected.

BI-3: the bot completion message named the Gemini ENGINE's voice
(ကိုသီဟ) while the app named the voice the user picked (ကိုမင်း).
The completion message now resolves through
recap_completion_voice_display_name — the picked persona's label,
Edge jobs keeping their honest Edge label. — REVERSED by Batch BK
the same day (owner decision 2026-11-06): the completion message
names the heard voice again via recap_voice_display_name, with a
polite explanatory note when the names differ; the BI-3 pins in
this file were flipped to the final rule, and the completion
resolver is pinned as retained-but-unused.

BI-4 (parent-directed addition): the REAL root cause of the TTS
balance-line bug — the bottom-nav handler in setupTabs() duplicates
switchTab's pane switching without calling it and carried no
tab-tts hook, so the quota loader never ran on nav entry (BH had
hardened a loader that was never called). The handler now carries
the same tab-tts -> refreshTtsQuota hook switchTab has.
"""
from __future__ import annotations

import inspect
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app" / "services" / "recap_engine"))

from core import (  # noqa: E402
    _clustered_glyph_row_scores,
    _frame_glyph_row_scores,
    _subtitle_band_boxes,
)
from app.services.downloader import MediaDownloader  # noqa: E402
from app.services.recap_engine import recap_pipeline as pipeline_mod  # noqa: E402
from app.services.voice_display import (  # noqa: E402
    recap_completion_voice_display_name,
    recap_voice_display_name,
)
from app.webapp import api as api_mod  # noqa: E402

APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
API_SRC = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
PIPELINE_SRC = (
    ROOT / "app/services/recap_engine/recap_pipeline.py"
).read_text(encoding="utf-8")

H = 200  # synthetic score-matrix height


def _matrix(n: int = 12) -> np.ndarray:
    return np.zeros((n, H))


def _covers(box: dict, lo: float, hi: float) -> bool:
    return box["y"] - box["h"] / 2.0 <= lo and box["y"] + box["h"] / 2.0 >= hi


# ── BI-1 (a): uniform scene noise forms no band ─────────────────────

def test_scene_noise_matrix_yields_zero_bands():
    # The zoo failure profile, distilled: glyph-ish scores on rows
    # all over the height (face / fence / jacket texture), several
    # slightly stronger patches, everything moving between samples
    # (dyn ratio high, so the static-drop rule cannot save us).
    # Batch BI separated this from captions with the prominence
    # gate; Batch BM removed that gate (it killed genuine weak
    # strips on busy real frames — see the module docstring) and
    # the rejection is now positional: every row is "loose" here,
    # so the texture collapses into ONE group spanning the whole
    # height, whose padded/capped extent lands mid-picture
    # (0.42-0.58) and is dropped by the caption-zone rule.
    S = np.zeros((12, H))
    for k in range(12):
        S[k, :] = 0.030 + 0.004 * ((k % 3) - 1)
    for y in range(0, H, 40):  # stronger "texture patches"
        for k in range(12):
            S[k, y:y + 8] = 0.05 if k % 2 == 0 else 0.04
    assert _subtitle_band_boxes(S) == []


# ── BI-1 (b): a genuine band still survives ─────────────────────────

def _caption_matrix() -> np.ndarray:
    """The Batch AI GlunZpix30I profile (see the AI suite): weak,
    intermittent bottom captions over a low noise floor."""
    S = _matrix()
    S[:, :] = 0.004
    for y in range(176, 186):
        S[1, y] = 0.013
        S[7, y] = 0.012
    for k, v in enumerate((0.030, 0.026, 0.033, 0.022, 0.028)):
        for y in range(186, 192):
            S[2 * k, y] = v
    S[9, 189] = 0.024
    return S


def test_genuine_bottom_band_survives():
    # Renamed from test_genuine_bottom_band_survives_prominence_gate
    # (Batch BM): the gate this pinned against is removed — it
    # killed this very profile's real-file counterpart on busy
    # frames (peak/median ~1.4x vs the 4x floor; BL drift
    # investigation). The band survives on the Batch AI rules plus
    # its in-zone (bottom) position.
    boxes = _subtitle_band_boxes(_caption_matrix())
    assert boxes, "the GlunZpix30I caption profile must still produce a band"
    assert any(_covers(b, 0.90, 0.955) for b in boxes), boxes


def test_weak_mid_picture_mush_dropped_by_zone_strong_bottom_kept():
    # Re-pinned from test_prominence_gate_compares_against_the_
    # frames_own_noise (Batch BM). Same fixture, same outcome,
    # different mechanism: Batch BI separated the two bands by
    # prominence (strong peak ~0.065 over the ~0.048 gate; mush
    # ~0.033 under it). The gate is gone — it also killed genuine
    # weak strips (ratio ~1.4x) — and Batch BM's caption-zone rule
    # separates them by position instead: the mush band (rows
    # 60-67, padded extent ~0.244-0.364) is mid-picture and is
    # zone-dropped; the strong band (rows 150-157) is in the
    # bottom zone and is kept.
    S = np.zeros((12, H))
    for k in range(12):
        S[k, :] = 0.012 if k % 2 == 0 else 0.008
    for k in range(12):  # strong band, rows 150-157
        S[k, 150:158] = 0.07 if k % 2 == 0 else 0.05
    for k in range(12):  # mush band, rows 60-67
        S[k, 60:68] = 0.035 if k % 2 == 0 else 0.025
    boxes = _subtitle_band_boxes(S)
    assert len(boxes) == 1, boxes
    assert _covers(boxes[0], 0.76, 0.79), boxes


# ── BI-1 (c): the cluster-run scorer ────────────────────────────────

def test_cluster_scorer_long_bar_scores_near_zero_strokes_score_high():
    g = np.full((100, 270), 40, dtype=int)
    # A long continuous bright bar with a dark outline nearby (a
    # fence rail / clothing block): its only mask pixels sit in the
    # two short edge runs, so the rows score ~0.
    g[38:52, 20:250] = 235
    g[36:38, :] = 25
    g[52:54, :] = 25
    # Clustered short strokes (subtitle glyphs): 3px bright / 3px
    # dark across the row.
    for y in range(64, 78):
        for x in range(270):
            g[y, x] = 235 if (x // 3) % 2 == 0 else 25
    scores = _frame_glyph_row_scores(g)
    assert scores[40:50].max() < 0.05, scores[40:50]
    assert scores[66:76].min() > 0.2, scores[66:76]


def test_cluster_scorer_counts_thin_strokes_but_not_long_runs():
    # The run window is 1-14 px (Batch BI, tuned on the genuine
    # GlunZpix30I strip: 53% of its mask mass is 1-px runs —
    # anti-aliased thin strokes — and none is over 14; a 2-px
    # minimum halved the real band's peak score and lost it).
    mask = np.zeros((4, 60), dtype=bool)
    mask[1, ::3] = True  # isolated 1-px strokes: 20 of 60 px
    scores = _clustered_glyph_row_scores(mask)
    assert scores[1] == pytest.approx(20 / 60)
    mask2 = np.zeros((4, 60), dtype=bool)
    for x0 in range(0, 60, 6):
        mask2[2, x0:x0 + 3] = True  # 3-px strokes count fully
    assert _clustered_glyph_row_scores(mask2)[2] == pytest.approx(0.5)
    mask3 = np.zeros((4, 60), dtype=bool)
    mask3[3, :] = True  # one 60-px continuous run: texture, scores 0
    assert _clustered_glyph_row_scores(mask3)[3] == 0.0


# ── BI-1 (d): the 2-band cap ────────────────────────────────────────

def test_band_cap_keeps_two_strongest():
    # Batch BM (owner Decision 2, 2026-11-06): the fixture's bands
    # were repositioned INTO the caption zones — the pre-BM
    # strongest band sat mid-picture (rows 90-97, extent
    # 0.394-0.514), where Batch BM's zone rule drops it before the
    # cap ever ranks. Cap mechanics are unchanged (zone drop
    # first, cap second — owner Option A ordering).
    # Batch BQ re-pin (owner decision 2026-11-06): the top zone
    # is REMOVED, so the fixture's top band is now zone-dropped
    # before the cap (kept here as the zone-drop witness) and a
    # third BOTTOM band (rows 128-135, weakest survivor) gives
    # the cap something to drop: the cap now ranks within the
    # bottom zone only.
    S = _matrix()

    def band(y0, peak):
        for k in range(12):
            S[k, y0:y0 + 8] = peak if k % 2 == 0 else peak * 0.7

    band(20, 0.10)   # old top zone (extent ~0.044-0.164) — zone-dropped (BQ)
    band(128, 0.12)  # bottom zone (extent ~0.584-0.704) — weakest survivor
    band(150, 0.20)  # bottom zone (extent ~0.694-0.814) — strongest
    band(170, 0.15)  # bottom zone (extent ~0.794-0.914)
    logs: list[str] = []
    boxes = _subtitle_band_boxes(S, log=logs.append)
    assert len(boxes) == 2, boxes
    ys = sorted(b["y"] for b in boxes)
    assert any(abs(y - 0.754) < 0.06 for y in ys), ys  # the 0.20 band
    assert any(abs(y - 0.854) < 0.06 for y in ys), ys  # the 0.15 band
    assert not any(abs(y - 0.104) < 0.06 for y in ys), ys  # top band gone
    assert not any(abs(y - 0.644) < 0.06 for y in ys), ys  # 0.12 cap-dropped
    assert any("caption-zone" in m for m in logs), logs
    assert any("2-band cap" in m for m in logs), logs


# ── BI-2: server defaults ───────────────────────────────────────────

def test_recap_config_auto_detect_defaults_on_and_persists():
    cfg = api_mod.RecapConfigRequest(url="https://example.com/v")
    assert cfg.blur_auto_detect is True
    assert cfg.model_dump()["blur_auto_detect"] is True
    off = api_mod.RecapConfigRequest(
        url="https://example.com/v", blur_auto_detect=False
    )
    assert off.model_dump()["blur_auto_detect"] is False
    # A stored pre-BI config (no key) re-parses to ON.
    legacy = {"url": "https://example.com/v", "blur_strength": 18}
    assert api_mod.RecapConfigRequest(**legacy).blur_auto_detect is True
    # The preview-composite model mirrors the default.
    assert api_mod.RecapPreviewCompositeRequest().blur_auto_detect is True


def test_user_prefs_auto_detect_default_on():
    assert api_mod._effective_user_prefs({})["recapBlurAutoDetect"] is True
    clean = api_mod._sanitize_user_prefs({"recapBlurAutoDetect": False})
    assert clean["recapBlurAutoDetect"] is False


def test_pipeline_and_downloader_signatures_default_on():
    for fn in (pipeline_mod.run_recap_pipeline, pipeline_mod.generate_recap_preview):
        param = inspect.signature(fn).parameters["blur_auto_detect"]
        assert param.default is True
    for fn in (MediaDownloader.generate_recap_video, MediaDownloader.generate_recap_preview):
        param = inspect.signature(fn).parameters["blur_auto_detect"]
        assert param.default is True


# ── BI-2: detector gating at both call sites ────────────────────────

def _make_tiny_video(path: Path, seconds: int = 2) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size=320x180:rate=15",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(path),
        ],
        check=True,
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_preview_skips_detector_when_auto_detect_off(tmp_path, monkeypatch):
    src = tmp_path / "tiny.mp4"
    _make_tiny_video(src)
    calls: list = []

    def spy(*a, **k):
        calls.append(1)
        return []

    monkeypatch.setattr(pipeline_mod, "detect_source_subtitle_bands", spy)
    monkeypatch.setattr(
        pipeline_mod, "extract_preview_with_overlays", lambda **kw: "ok"
    )
    pipeline_mod.generate_recap_preview(
        src, tmp_path / "off.jpg", blur_auto_detect=False, at_seconds=0.5
    )
    assert calls == [], "toggle OFF must not invoke the detector (preview)"
    pipeline_mod.generate_recap_preview(src, tmp_path / "on.jpg", at_seconds=0.5)
    assert calls, "default ON must invoke the detector (preview)"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_render_skips_detector_when_auto_detect_off(tmp_path, monkeypatch):
    src = tmp_path / "tiny.mp4"
    _make_tiny_video(src)
    calls: list = []

    def spy(*a, **k):
        calls.append(1)
        return []

    class FakeBuilder:
        def __init__(self, **kwargs):
            pass

        def build(self, *, source, job_dir, source_title, reuse=True):
            plan_path = Path(job_dir) / "plan.json"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text("{}", encoding="utf-8")
            return plan_path, {"segments": []}

    class FakeRunner:
        def __init__(self, **kwargs):
            pass

        def run(self):
            return {
                "final": "x.mp4", "audio": None, "subtitles_srt": None,
                "segments": 0, "duration": 0.0,
            }

    monkeypatch.setattr(pipeline_mod, "RecapPlanBuilder", FakeBuilder)
    monkeypatch.setattr(pipeline_mod, "EdgeSmartSync", FakeRunner)
    monkeypatch.setattr(pipeline_mod, "detect_source_subtitle_bands", spy)
    pipeline_mod.run_recap_pipeline(
        src, job_dir=tmp_path / "job_off", blur_auto_detect=False
    )
    assert calls == [], "toggle OFF must not invoke the detector (render)"
    pipeline_mod.run_recap_pipeline(src, job_dir=tmp_path / "job_on")
    assert calls, "default ON must invoke the detector (render)"


# ── BI-2: client + API wiring ───────────────────────────────────────

def test_auto_detect_frontend_wiring():
    # The switch lives in the blur settings group, default checked,
    # labelled + hinted through i18n (both languages).
    group = INDEX_HTML.split('id="recap-blur-group"', 1)[1].split(
        'id="recap-title-toggle"', 1
    )[0]
    assert 'id="recap-blur-autodetect-toggle"' in group
    assert "recap_blur_autodetect_label" in group
    assert "recap_blur_autodetect_hint" in group
    assert I18N_JS.count("recap_blur_autodetect_label") == 2
    assert I18N_JS.count("recap_blur_autodetect_hint") == 2
    # State default ON; the key rides the persisted-settings bools,
    # the account prefs, the History prefill, and BOTH payloads.
    assert "recapBlurAutoDetect: true," in APP_JS
    assert '"recapBlurAutoDetect"' in APP_JS  # RECAP_SETTING_BOOLS / prefs keys
    assert APP_JS.count("blur_auto_detect: state.recapBlurAutoDetect !== false") == 2
    assert "recapBlurAutoDetect: cfg.blur_auto_detect !== false" in APP_JS
    # The API forwards the field at both call sites and the engine
    # gates both detector call sites on it.
    assert API_SRC.count("blur_auto_detect=bool(req.blur_auto_detect)") == 2
    assert PIPELINE_SRC.count("blur_auto_detect: bool = True") == 2
    assert "and blur_auto_detect:" in PIPELINE_SRC  # render gate
    assert "if _p_user_box is None and blur_auto_detect:" in PIPELINE_SRC


# ── BI-3: completion message voice name (REVERSED by Batch BK) ──────
# BI-3 made the completion message name the PICKED persona; the owner
# reversed that the same day (2026-11-06): the message names the voice
# ACTUALLY USED (the heard-voice resolver — the pre-BI behaviour),
# with a polite note when the names differ (Batch BK's note pins live
# in test_batch_bk_2026_11_06.py). The pins below were flipped to the
# final rule.

def test_completion_name_gemini_job_uses_heard_voice():
    # The Milo case: engine gemini, Gemini voice Charon (ကိုသီဟ),
    # persona the user picked = komin. The completion line names the
    # voice that actually read the recap: ကိုသီဟ.
    name = recap_voice_display_name("gemini", "Charon", "komin")
    assert name == "ကိုသီဟ"
    assert recap_voice_display_name("gemini", "Charon", "mayoon") == "ကိုသီဟ"


def test_completion_resolver_retained_with_picked_name_behaviour():
    # recap_completion_voice_display_name stays in voice_display.py
    # with its BI behaviour (the picked persona's name) — Batch BK
    # only stopped the notification from calling it.
    assert recap_completion_voice_display_name("gemini", "Charon", "komin") == "ကိုမင်း"
    assert recap_completion_voice_display_name("gemini", "Charon", "mayoon") == "မယွန်း"


def test_completion_name_edge_and_voxcpm_unchanged():
    # Edge keeps its honest engine label (Batch AW); VoxCPM names the
    # persona (the heard voice IS the persona there).
    assert (
        recap_voice_display_name("edge", None, "komin")
        == "🎧 Edge TTS · ကိုသီဟ"
    )
    assert (
        recap_voice_display_name("edge_tts", None, "mathiri")
        == "🎧 Edge TTS · မနီလာ"
    )
    assert recap_voice_display_name("voxcpm", None, "komin") == "ကိုမင်း"
    assert recap_voice_display_name(None, None, "komin") == "ကိုမင်း"


def test_completion_notify_uses_heard_voice_resolver():
    assert (
        "recap_voice_display_name(tts_engine, gemini_voice, req.voice_slug)"
        in API_SRC
    )
    # The picked-name completion resolver is no longer referenced by
    # the webapp API at all (Batch BK reversal).
    assert "recap_completion_voice_display_name" not in API_SRC


# ── BI-4: the nav handler must run the TTS quota loader ─────────────
# Root cause (parent-directed addition, found 2026-11-06 after BH
# shipped): the bottom-nav click handler in setupTabs() is a
# hand-rolled duplicate of switchTab's pane switching — it does NOT
# call switchTab(). It carried per-tab hooks for tab-history and
# tab-admin but none for tab-tts, while the AW-era refreshTtsQuota
# call lived only inside switchTab() — so tapping the TTS nav button
# never ran the loader and the balance line never painted on entry
# (BB's and BH's loader hardening could not fire). Both entry paths
# must carry the hook.

def test_setup_tabs_handler_has_tts_quota_hook():
    handler = APP_JS.split("function setupTabs()", 1)[1].split(
        "async function loadUserProfile", 1
    )[0]
    # The handler still does its own pane switching (no delegation).
    assert "loadUserHistory();" in handler  # the history hook pattern
    assert 'if (targetTab === "tab-tts" && typeof window.refreshTtsQuota === "function") {' in handler
    assert "window.refreshTtsQuota();" in handler


def test_switch_tab_retains_tts_quota_hook():
    body = APP_JS.split("function switchTab(tabId)", 1)[1].split(
        "window.switchTab = switchTab;", 1
    )[0]
    assert 'if (tabId === "tab-tts" && typeof window.refreshTtsQuota === "function") {' in body
    assert "window.refreshTtsQuota();" in body
