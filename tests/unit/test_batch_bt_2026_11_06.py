"""Batch BT — the two recap-form defects Batch BR's eyes-on pass
surfaced (2026-11-06 evening).

BT-A (studio inline status line never updates): Batch BR made the
recap DETAIL-box lines render from the LiveJobs store, and the pill
+ detail card then tracked stages together — but the line the user
actually watches after a 1-Click submit is the slim STUDIO INLINE
line (#recap-inline-status / #recap-inline-status-text), because
Batch AC keeps the detail box closed. That line was written exactly
once — showStudioInlineStatus("recap", t("recap_stage1_status"))
at submit — and nothing ever wrote it again: live, it sat on the
stage-1 "[1/4] transcribing…" text for a whole run and even after
the job finished (job rc_1793978690_f73460, SUCCESS). The fix
paints the inline line from the SAME store renderer BR built:

- renderRecapFormProgress also sets #recap-inline-status-text to
  liveJobStageText(job) for the tracked job and un-hides the line;
- the paint sits behind the renderer's two existing guards — the
  tracked-job guard (another job's store update cannot repaint it)
  and the recapFormNotice early-return (a parked poller health
  notice owns the status text, inline included);
- terminal jobs paint their outcome text like the pill (no
  terminal special-case on the inline line; the result/failed
  boxes stay recapPollTick's business);
- the store-removed branch empties the inline text too, mirroring
  the detail lines — no surface may keep a stale stage frozen;
- RECAP only: the voice inline status flow is untouched.

BT-B (subtitle margin does not restore): live, a subtitle margin
set to 20% (with the "settings saved to account" toast) came back
as the 5% default on fresh loads, while the master Blur toggle —
flipped in the same session — DID persist. Traced asymmetry: the
margin can be set by DRAGGING the subtitle box on the canvas;
enableVerticalDrag/enableHorizontalDrag drive the slider through
raw mouse + touch events with preventDefault, so a drag ends in a
pointerup but NO click/change/input ever reaches the recap tab.
The two delegated savers disagreed about that event:

- the localStorage snapshot saver (saveRecapSettingsSoon) listens
  for ["click", "change", "input", "pointerup"] — a drag WAS
  snapshotted;
- the account-prefs saver (saveAccountPrefsSoon, setupAccountPrefs)
  listened for ["click", "change", "input"] only — a drag was NEVER
  sent to the server (never-saved at the account layer);
- on the next launch the boot restore painted the snapshot's 20,
  then loadAccountPrefs/applyAccountPrefs overwrote state, the
  snapshot (cacheAccountPrefs write-through) and the slider with
  the server's stale 5 (applied-then-overwritten).

The Blur toggle persisted because flipping it is a click/change
gesture the prefs saver does hear. The fix is the smallest correct
link: pointerup joins the prefs saver's recap-tab event list —
event parity with the snapshot saver — so a drag-end schedules the
same debounced, change-compared (BB-8) prefs save a click would,
and collectAccountPrefs carries the dragged margin (read live from
the slider) to the server. The same one line also closes the gap
for the other drag-set account prefs (sub_x, sub_w, title X/Y).
Nothing else moves: the slider remains the margin for the canvas
drag math (AK), the submit payload still reads the slider, the
server prefs schema/default (5 when never saved) is unchanged, and
the BR title-toggle session-truth is untouched.

Invariants pinned here: the inline paint + its guard ordering +
removed-branch mirroring + voice isolation + the submit seed; the
prefs/snapshot event parity; the margin's collect expression; the
never-saved default at both layers; the server half of the
save → fresh-load cycle; and cache token r91.
"""
from __future__ import annotations

from pathlib import Path

from app.webapp import api as api_mod


STATIC_DIR = Path(api_mod.__file__).resolve().parent / "static"
APP_JS = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _between(start_marker: str, end_marker: str) -> str:
    start = APP_JS.index(start_marker)
    end = APP_JS.index(end_marker, start)
    return APP_JS[start:end]


def _renderer_body() -> str:
    return _between(
        "function renderRecapFormProgress(job) {",
        "function ensureRecapFormStoreSubscription()",
    )


def _subscription_body() -> str:
    return _between(
        "function ensureRecapFormStoreSubscription() {",
        "function startRecapJobPolling(jobId)",
    )


def _prefs_setup_body() -> str:
    return _between(
        "function setupAccountPrefs() {",
        '// -- 401 "Open via Telegram" modal',
    )


def _collect_prefs_body() -> str:
    return _between(
        "function collectAccountPrefs() {",
        "function cacheAccountPrefs(prefs)",
    )


# ── BT-A: the inline line renders from the store renderer ────────

def test_bt_renderer_paints_inline_line_from_store_job():
    body = _renderer_body()
    # The inline line is painted with the pill's own text function
    # on the same merged store job as the detail stage line.
    # Batch CB: both lines now paint the composed formStatusText —
    # which IS liveJobStageText(job) except while the poller's
    # overrun flag is set (Item 4's overrun sentence takes both).
    assert 'const inlineLine = document.getElementById("recap-inline-status");' in body
    assert 'const inlineText = document.getElementById("recap-inline-status-text");' in body
    assert "if (inlineText) inlineText.innerText = formStatusText;" in body
    # …and kept visible while a job is tracked.
    assert 'if (inlineLine) inlineLine.classList.remove("hidden");' in body
    # Both status surfaces use the identical text source.
    assert body.count("innerText = formStatusText;") == 2
    assert ": liveJobStageText(job);" in body


def test_bt_inline_paint_sits_behind_tracked_job_and_notice_guards():
    body = _renderer_body()
    # Another job's store update can never repaint the inline line:
    # the tracked-job guard precedes every paint in the renderer.
    guard = body.index("if (!job || job.id !== recapFormJobId) return;")
    paint = body.index("if (inlineText) inlineText.innerText = formStatusText;")
    assert guard < paint
    # A parked poller health notice owns the status text — inline
    # included: the notice early-return also precedes the paint.
    notice = body.index("if (recapFormNotice) return;")
    assert notice < paint


def test_bt_inline_terminal_paints_outcome_like_the_pill():
    body = _renderer_body()
    paint = body.index("if (inlineText) inlineText.innerText = formStatusText;")
    # No terminal special-case parks the inline line on its last
    # in-progress text: the unconditional paint (the store job's
    # outcome text on a terminal job, exactly as the pill renders)
    # comes before the step line's own terminal-clearing block, and
    # the renderer never hides the inline line.
    terminal_block = body.index("if (liveJobsIsTerminal(job.status)) {")
    assert paint < terminal_block
    assert 'inlineLine.classList.add("hidden")' not in body
    assert 'inlineText.innerText = "";' not in body


def test_bt_removed_store_job_empties_inline_text_too():
    sub = _subscription_body()
    removed = sub.index("if (opts && opts.removed) {")
    inline = sub.index('const inlineText = document.getElementById("recap-inline-status-text");')
    empty = sub.index('if (inlineText) inlineText.innerText = "";')
    # Inside the removed branch, for the tracked job only — mirroring
    # the detail lines so no surface keeps a stale stage frozen.
    assert removed < inline < empty
    assert "if (job.id === recapFormJobId) {" in sub


def test_bt_voice_inline_status_flow_untouched():
    body = _renderer_body()
    # Recap renderer scope: no voice elements, no voice writes.
    assert "voice-inline-status" not in body
    assert "voice-inline-status" not in _subscription_body()
    # The voice studio still seeds its own inline line at submit.
    assert 'showStudioInlineStatus("voice", t("preparing_msg"));' in APP_JS


def test_bt_submit_still_seeds_stage1_inline_text():
    # The submit-time seed stays (it is the line's first paint before
    # the store's first notify); the renderer takes over from there.
    assert 'showStudioInlineStatus("recap", t("recap_stage1_status"));' in APP_JS
    assert 'function showStudioInlineStatus(kind, text) {' in APP_JS


# ── BT-B: pointerup parity for the account-prefs saver ───────────

def test_bt_prefs_saver_hears_pointerup_on_recap_tab():
    body = _prefs_setup_body()
    # The recap binding now carries the full event set, pointerup
    # included — a canvas drag-end schedules the same prefs save a
    # click would.
    assert ('["click", "change", "input", "pointerup"].forEach(evt => '
            'recapTab.addEventListener(evt, saveAccountPrefsSoon));') in body


def test_bt_snapshot_and_prefs_savers_have_event_parity():
    # The reference set the prefs saver was missing pointerup from:
    # the snapshot saver's binding is unchanged and identical.
    assert ('["click", "change", "input", "pointerup"].forEach(evt =>\n'
            '            recapTabEl.addEventListener(evt, saveRecapSettingsSoon));') in APP_JS


def test_bt_voice_tab_prefs_binding_unchanged():
    body = _prefs_setup_body()
    # No canvas drags exist on the voice tab — its binding keeps the
    # original three events (fix scope is the recap tab only).
    assert ('["click", "change", "input"].forEach(evt => '
            'voiceTab.addEventListener(evt, saveAccountPrefsSoon));') in body


def test_bt_collect_carries_the_dragged_margin_from_the_slider():
    body = _collect_prefs_body()
    # The pointerup-triggered save collects the margin live from the
    # slider element — the very element the drag helpers write.
    assert ('recapSubMargin: sy ? parseInt(sy.value) : '
            '(state.recapSubMargin != null ? state.recapSubMargin : 5),') in body
    assert 'const sy = document.getElementById("slider-sub-y");' in body


def test_bt_margin_never_saved_default_stays_five():
    # Client shipped default…
    assert "    recapSubMargin: 5," in APP_JS
    # …and the server default for an account that never saved one.
    assert api_mod.DEFAULT_USER_PREFS["recapSubMargin"] == 5
    assert api_mod._effective_user_prefs(None)["recapSubMargin"] == 5
    assert api_mod._effective_user_prefs({})["recapSubMargin"] == 5
    # The stored range is untouched.
    assert api_mod.USER_PREF_INTS["recapSubMargin"] == (0, 95)


def test_bt_server_half_of_save_then_fresh_load_cycle():
    # What the fixed saver PUTs (sanitized) is what the next fresh
    # load's GET hands back to applyAccountPrefs — the margin rides
    # the whole server half of the cycle unchanged.
    clean = api_mod._sanitize_user_prefs(
        {"recapSubMargin": 20, "recapSettingsVersion": 2})
    assert clean["recapSubMargin"] == 20
    eff = api_mod._effective_user_prefs(clean)
    assert eff["recapSubMargin"] == 20
    # A later partial save that omits the margin (the PUT merge
    # keeps stored keys) cannot drop it back to the default.
    merged = dict(clean)
    merged.update(api_mod._sanitize_user_prefs({"recapBlurSubs": False}))
    assert api_mod._effective_user_prefs(merged)["recapSubMargin"] == 20


# ── Cache token ──────────────────────────────────────────────────

def test_bt_cache_token_is_r91():
    assert INDEX_HTML.count("20261104_r117") == 4
    assert "20261104_r90" not in INDEX_HTML
