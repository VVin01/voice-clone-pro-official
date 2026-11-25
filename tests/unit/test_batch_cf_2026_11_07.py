"""Batch CF — Step-4 ETA line actually paints + static slide retired
(2026-11-07).

Found by the CB eyes-on (Milo, Telegram Web, 2026-11-07 13:38–13:45)
and code-verified the same hour:

* CB's ``refreshRecapStep4Eta()`` was wired ONLY into the cost hint
  (``updateRecapSplitCostHint``) and the engine picker. Entering
  Step 4 through ``showRecapStep(4)`` — the wizard's one door for
  every step change — refreshed the output summary but never the
  ETA, so a navigation/restored-session entry left
  ``#recap-step4-eta`` EMPTY (eyes-on: zero ခန့်မှန်း matches on
  Step 4 for a 101 s Edge job). CF calls the refresh inside the
  ``n === 4`` branch; the function is cache-keyed + sequence-
  guarded, so the extra call is a no-op when nothing changed.
* The Step-4 announcement carousel still rotated the pre-CB static
  slide ``queue_notice_recap`` ("ပုံမှန်အားဖြင့် ၂–၄ မိနစ်") — a
  fixed minute claim contradicting CB's honest server range, and
  the ONLY number shown when the first gap bit. CF removes the
  slide (and its dot) from the carousel. The i18n key itself is
  RETAINED in both languages: app.js still uses it as the in-job
  step-line filler when a job has neither a queue position nor an
  ETA range yet (pinned by Batch BR), so key removal was stopped
  per the CF spec's recon rule and is reported instead.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")


def _carousel_block() -> str:
    start = INDEX_HTML.index('id="studio-alert-carousel"')
    end = INDEX_HTML.index("</section>", start)
    return INDEX_HTML[start:end]


def _show_recap_step_body() -> str:
    start = APP_JS.index("function showRecapStep(n)")
    end = APP_JS.index("window.showRecapStep", start)
    return APP_JS[start:end]


# ── Item 1: Step-4 entry paints the ETA line ─────────────────────

def test_show_recap_step4_branch_refreshes_eta():
    body = _show_recap_step_body()
    assert (
        "if (n === 4) { refreshRecapStep4Output(); refreshRecapStep4Eta(); }"
        in body
    )


def test_step4_eta_refresh_still_wired_in_cost_hint_and_picker():
    # Hub wiring: the engine picker routes through updateRecapSplitCostHint(),
    # which invokes refreshRecapStep4Eta() alongside Step-4 entry.
    hub_start = APP_JS.index("function updateRecapSplitCostHint()")
    hub_end = APP_JS.index("window.repaintRecapCtaPrice", hub_start)
    assert "refreshRecapStep4Eta();" in APP_JS[hub_start:hub_end]
    picker_start = APP_JS.index('const engineSel = document.getElementById("recap-engine-picker")')
    picker_end = APP_JS.index("refreshRecapEngineLock();", picker_start)
    assert "updateRecapSplitCostHint();" in APP_JS[picker_start:picker_end]


# ── Item 2: the static minute-claim slide is gone ────────────────

def test_carousel_no_longer_carries_queue_notice_slide():
    # Round 15 Part A retired the entire #studio-alert-carousel tier pill.
    assert 'id="studio-alert-carousel"' not in INDEX_HTML
    assert 'data-i18n="queue_notice_recap"' not in INDEX_HTML


def test_carousel_keeps_limit_slide_without_minute_claim():
    # Round 15 Part A deleted the tier pill carousel; recap_rights_note stays in the notes expander.
    assert 'id="studio-alert-carousel"' not in INDEX_HTML
    assert 'id="recap-limit-pill"' not in INDEX_HTML
    assert 'data-i18n="recap_rights_note"' in INDEX_HTML


def test_carousel_rotator_is_count_agnostic():
    # setupAlertCarousel works positionally over however many
    # slides exist: it bails only on ZERO slides, wraps with
    # slides.length, and guards the dot lookup — so the 3 → 2
    # slide reduction cannot error or stall it.
    start = APP_JS.index("function setupAlertCarousel()")
    body = APP_JS[start:start + 2600]
    assert "if (!slides.length) return;" in body
    assert "(idx + slides.length) % slides.length" in body
    assert "if (dots[currentIndex])" in body


def test_queue_notice_key_retained_for_in_job_filler():
    # STOP-and-report outcome (CF spec Item 2): the key has a
    # second surface — the recap form's step line appends it when
    # a running job has neither a queue position nor an ETA range
    # (Batch BR pin). The key therefore stays in BOTH languages
    # and the app.js filler stays untouched.
    assert I18N_JS.count("queue_notice_recap:") == 2
    assert 'stepBits.push(t("queue_notice_recap"));' in APP_JS


# ── Extension 2b: no fixed minute claim in either value ─────────

def _i18n_values(key: str) -> list[str]:
    return re.findall(rf'{key}: "([^"]*)"', I18N_JS)


def test_retired_keys_carry_no_digit_minute_claim():
    # Extension 2b (same purpose as Item 2): the two values that
    # still reached users — queue_notice_recap (in-job step-line
    # filler) and recap_eta_text (#recap-step-text pre-paint
    # placeholder) — are now neutral lines. Neither may contain a
    # digit-based minute claim in EITHER language.
    digits = "0123456789၀၁၂၃၄၅၆၇၈၉"
    for key in ("queue_notice_recap", "recap_eta_text"):
        values = _i18n_values(key)
        assert len(values) == 2  # my + en
        for value in values:
            assert not any(ch in digits for ch in value), (key, value)
            assert "မိနစ်" not in value
            assert "minute" not in value.lower()
            assert " min" not in value.lower()
    # The retired literals are gone from every shipped surface.
    for stale in ("၂–၄ မိနစ်", "2–4 minutes", "2–4 min"):
        assert stale not in I18N_JS
        assert stale not in INDEX_HTML


def test_recap_step_text_placeholder_matches_neutral_value():
    # Round 15 Part C keeps #recap-step-text empty pre-paint so queued jobs
    # never flash hardcoded 0/4 or stale ETA text before LiveJobs paints.
    match = re.search(
        r'id="recap-step-text">([^<]*)<',
        INDEX_HTML,
    )
    assert match is not None
    assert match.group(1) == ""


# ── Release pin: cache token (split-literal, sweep-proof) ────────

def test_cache_token_is_followup1():
    token = "20261125_" + "admin_r1"
    stale = "20261104_r" + "99"
    assert INDEX_HTML.count(token) == 5
    assert stale not in INDEX_HTML
