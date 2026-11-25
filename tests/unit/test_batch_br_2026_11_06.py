"""Batch BR — the two remaining Batch AK leftovers (owner wrap-up
2026-11-06 21:05: finish Batch AK alongside the settings audit).

BR-1 (form progress line vs status pill): the recap form's stage
line (#recap-status-msg), step line (#recap-step-text), fill and
stage stepper used to be painted ONLY by the per-job detail poller
(recapPollTick) from its private /api/recap/{id}/status response —
its own 2s-to-5s cadence, its own monotonic clamp — while the pill
(#live-jobs-pill-stage) renders from the LiveJobs store on a
separate /api/jobs/live chain. The two surfaces routinely sat one
step apart (recon 2026-11-06 section 3a). The fix makes the store
the one source of truth for the form lines too:

- renderRecapFormProgress(job) paints the lines from the merged
  store job — liveJobStageText(job) for the stage line (the pill's
  own text function) and stage_index/stage_total for the step line
  — driven by a LiveJobs.subscribe registration made when the
  detail poller starts tracking a job;
- recapPollTick loses its step/stage/fill writes AND its private
  recapPollMaxStep/recapPollMaxPercent clamp (monotonicity is the
  store merge's job now). It keeps everything the bulk endpoint
  does not carry: result/download URLs, the script editor,
  failure/refund handling, its health bookkeeping — and the ETA
  extra (Batch L), which it caches into recapFormEtaSeconds for
  the store renderer instead of writing the step line itself;
- its terminal-outcome write STAYS, because it already uses the
  identical text source (liveJobStageText) — Batch AK Addition C;
- the two poller health notices (reconnecting cue, 60-minute
  background notice) still own the status line while parked: they
  set recapFormNotice, the store renderer defers to it, and a
  successful tick clears it;
- when no live job exists the renderer never runs (no subscription
  notify, no tracked job) — the lines keep their shipped state.

BR-2 (title toggle silently restored ON): the fresh default is
recapTitleEnabled: false, but restoreRecapSettings() re-applied a
saved recapTitleEnabled from the vcp_recap_settings snapshot on
every load (it sat in RECAP_SETTING_BOOLS), and applyAccountPrefs
applied the server-saved value into state, whose checkbox re-sync
in restore then showed ON — a form's loaded state masquerading as
its default (the owner's standing complaint; Batch AX
session-truth principle). The toggle is now SESSION-TRUTH:

- "recapTitleEnabled" is out of RECAP_SETTING_BOOLS, so restore
  never applies it from a snapshot (a snapshot still carrying the
  key is handled gracefully — the key is simply never read);
- it is also out of applyAccountPrefs' applied recap keys, so
  server prefs cannot resurrect it through state either (collect
  still SENDS the session value; the in-use change handler is
  untouched);
- prefillRecapFromHistory — an explicit re-create action — still
  sets the toggle from the past job's config, now DIRECTLY after
  the restore (the script-review toggle pattern), because restore
  no longer applies its snapshot's bool;
- every other snapshot setting (title text/position included)
  restores exactly as before.

Invariants pinned here: the store wiring + renderer composition,
the poller's removed writes and surviving duties, the notice
mechanism, the session-truth bool set + prefs keys + fresh
default, the prefill direct set ordered after restore, and cache
token r89.
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


def _poller_body() -> str:
    return _between(
        "function startRecapJobPolling(jobId) {",
        "function openRecapScriptEditor(",
    )


def _tick_body() -> str:
    return _between("const recapPollTick = async () => {",
                    "setInterval(recapPollTick, 2000)")


def _restore_body() -> str:
    return _between("function restoreRecapSettings() {",
                    "const recapTabEl = document.getElementById(\"tab-recap\");")


def _prefill_body() -> str:
    return _between("window.prefillRecapFromHistory = function(cfg, item) {",
                    "// Initial canvas mockup sync")


# ── BR-1a: the form lines render from the LiveJobs store ─────────

def test_br_form_lines_subscribe_to_livejobs_store():
    sub = _subscription_body()
    # The renderer is driven by the store's existing subscribe
    # mechanism — the same notify stream the pill renders on.
    assert "LiveJobs.subscribe((job, opts) => {" in sub
    assert "renderRecapFormProgress(job);" in sub
    # Voice jobs never reach the recap form renderer.
    assert 'if (!job || job.kind !== "recap") return;' in sub


def test_br_renderer_uses_pill_text_source_and_store_step_fields():
    body = _renderer_body()
    # Stage line: the pill's OWN text function on the store job.
    # Batch CB: the line is composed through formStatusText, which IS
    # liveJobStageText(job) except while the poller's overrun flag is
    # set (the Item-4 overrun sentence takes the line instead).
    assert "const formStatusText = (recapFormEtaOverrun && !liveJobsIsTerminal(job.status))" in body
    assert "? liveJobOverrunText(job)" in body
    assert ": liveJobStageText(job);" in body
    assert "if (statusMsg) statusMsg.innerText = formStatusText;" in body
    # Step line: composed from the store job's resolved stage (Round 15 Part C avoids
    # rendering "0/4" on queued jobs before real stage data arrives).
    assert '`${t("recap_step_label")} ${resolvedStage}/${stageTotal}`' in body
    # Queue position comes from the store job too (the bulk payload
    # carries it); the boilerplate fallback matches the old line.
    assert 'tf("recap_queue_ahead", { count: job.queue_position })' in body
    assert 'stepBits.push(t("queue_notice_recap"));' in body
    # Fill + stage stepper follow the same store job (Quality Pass §2F removes the 10% floor).
    assert "paintProgressBarFill(progressFill, job);" in body
    assert "setRecapStage(`${resolvedStage}/${stageTotal}`, job.status, job.progress_stage);" in body
    # Terminal store jobs clear the step line (the outcome text
    # itself comes from liveJobStageText, as on the pill).
    assert "if (liveJobsIsTerminal(job.status)) {" in body
    assert 'stepText.innerText = "";' in body


def test_br_renderer_only_paints_the_tracked_job():
    body = _renderer_body()
    assert "if (recapFormJobId && job.id && job.id !== recapFormJobId) return;" in body
    # Poller start is what marks the tracked job and arms the
    # subscription (every entry route — submit, resume, History —
    # funnels through startRecapJobPolling).
    poller = _poller_body()
    assert "recapFormJobId = jobId;" in poller
    assert "ensureRecapFormStoreSubscription();" in poller
    # A job already in the store paints immediately on poller start.
    assert "const storeJobOnStart = LiveJobs.jobs.get(jobId);" in poller
    assert "if (storeJobOnStart) renderRecapFormProgress(storeJobOnStart);" in poller


def test_br_removed_store_job_empties_the_form_lines():
    sub = _subscription_body()
    # When the server stops reporting the tracked job, the lines go
    # empty rather than freezing on a stale step.
    assert "if (opts && opts.removed) {" in sub
    assert "if (job.id === recapFormJobId) {" in sub
    assert 'if (statusMsg) statusMsg.innerText = "";' in sub
    assert 'if (stepText) stepText.innerText = "";' in sub


# ── BR-1b: the detail poller no longer writes step/stage ─────────

def test_br_tick_has_no_private_step_stage_writes_left():
    tick = _tick_body()
    # Nothing in the tick reads the detail payload's step/stage
    # fields any more — those were the divergent private source.
    assert "data.progress_step" not in tick
    assert "data.progress_stage" not in tick
    assert "data.progress_label" not in tick
    assert "data.progress_sub" not in tick
    assert "data.progress_percent" not in tick
    assert "progressFill" not in tick
    # The private monotonic clamp is gone from the whole file (the
    # store merge owns monotonicity now); the declarations must not
    # survive anywhere.
    assert "let recapPollMaxStep" not in APP_JS
    assert "let recapPollMaxPercent" not in APP_JS
    assert "_stepParts" not in APP_JS


def test_br_tick_keeps_its_remaining_duties():
    tick = _tick_body()
    # The poller itself is alive, on its original cadence anchors.
    assert "const recapPollTick = async () => {" in APP_JS
    assert "setInterval(recapPollTick, 2000)" in APP_JS
    # Result/download URLs, the script editor, terminal handling.
    assert "data.download_urls" in tick
    assert "openRecapScriptEditor(jobId);" in tick
    assert 'data.status === "SUCCESS"' in tick
    assert 'data.status === "FAILED"' in tick
    assert 'data.status === "CANCELLED"' in tick
    # The terminal outcome write stays — and uses the identical
    # text source as the store renderer (liveJobStageText).
    assert 'if (statusMsg) statusMsg.innerText = liveJobStageText({ status: data.status, kind: "recap" });' in tick
    # Health bookkeeping survives: failure streak, 404-gone, cap.
    poller = _poller_body()
    assert "function noteRecapPollFailure() {" in poller
    assert "function stopRecapPollingJobGone() {" in poller
    assert "RECAP_POLL_MAX_TICKS" in poller


def test_br_tick_repaints_from_the_store_job_and_caches_eta():
    tick = _tick_body()
    # The ETA extra (Batch L) is cached for the store renderer —
    # the one detail-payload field the step line still consumes.
    # Batch CB: the extra is now the remaining RANGE + the overrun
    # flag (the single-point eta_seconds and its 1-minute display
    # floor are retired — see the CB test file for the floor pins).
    assert "recapFormEtaRange = Array.isArray(data.eta_range_seconds) ? data.eta_range_seconds : null;" in tick
    assert "recapFormEtaOverrun = !!data.eta_overrun;" in tick
    assert 'formatEtaRangeText(recapFormEtaRange, "remaining")' in _renderer_body()
    # The repaint reads the STORE job, never the tick's response.
    assert "const storeJobForPaint = LiveJobs.jobs.get(jobId);" in tick
    assert "if (storeJobForPaint) renderRecapFormProgress(storeJobForPaint);" in tick
    # A successful tick hands the lines back to the store renderer.
    assert "recapFormNotice = null;" in tick
    assert "if (recapFormNotice) recapFormNotice = null;" not in tick


def test_br_poller_notices_own_the_status_line_while_parked():
    poller = _poller_body()
    # Reconnecting cue + background notice still park on the line,
    # now flagged so the store renderer defers to them.
    assert 'recapFormNotice = t("poll_reconnecting");' in poller
    assert 'recapFormNotice = t("poll_background_notice");' in poller
    # The renderer refuses to paint over a parked notice.
    assert "if (recapFormNotice) return;" in _renderer_body()
    # Poller start resets the notice/ETA state for the new job.
    # (Batch CB: the ETA state is the range + overrun flag pair.)
    assert "recapFormEtaRange = null;" in poller
    assert "recapFormEtaOverrun = false;" in poller
    assert "recapFormNotice = null;" in poller


# ── BR-2: the title toggle is session-truth ──────────────────────

def test_br_title_enabled_absent_from_restore_applied_bools():
    line = next(l for l in APP_JS.splitlines() if "const RECAP_SETTING_BOOLS" in l)
    assert '"recapTitleEnabled"' not in line
    # Every other bool still restores from the snapshot.
    for key in ("recapSyncCrop", "recapMirror", "recapBurnSubs",
                "recapBlurSubs", "recapBlurAutoDetect", "recapLogo",
                "recapBlurBoxTouched"):
        assert f'"{key}"' in line
    # The restore loop itself is unchanged — it applies exactly
    # the bool set above, and the snapshot still saves through it.
    assert "RECAP_SETTING_BOOLS.forEach(k => { if (typeof saved[k] === \"boolean\") state[k] = saved[k]; });" in APP_JS
    assert "RECAP_SETTING_BOOLS.forEach(k => { snap[k] = !!state[k]; });" in APP_JS


def test_br_restore_ignores_a_snapshot_title_enabled_true():
    # Batch DD: the title feature is removed entirely — restore
    # has no title keys to read, and no title state exists at all.
    body = _restore_body()
    assert "recapTitle" not in body
    assert "recapTitle" not in APP_JS


def test_br_account_prefs_cannot_resurrect_the_toggle():
    # Batch DD: there is no toggle left to resurrect — no title
    # key appears in collect, apply, or anywhere in the client.
    assert "recapTitle" not in APP_JS


def test_br_title_toggle_assignments_are_only_change_handler_and_prefill():
    # Batch DD: zero assignments — the flag itself is gone.
    assert APP_JS.count("state.recapTitleEnabled") == 0


def test_br_prefill_still_sets_title_from_job_config():
    body = _prefill_body()
    # Batch DD: prefill maps the DD config fields (protection,
    # blur-box list) and no title keys at all.
    assert "recapTitle" not in body
    assert "recapProtection: RECAP_LOCKED_PROTECTION," in body
    assert "if (srToggle) srToggle.checked = !!cfg.script_review;" in body


# ── Token ────────────────────────────────────────────────────────

def test_br_cache_token_is_r89():
    assert INDEX_HTML.count("20261125_admin_r1") == 5
    assert "20261104_r88" not in INDEX_HTML
