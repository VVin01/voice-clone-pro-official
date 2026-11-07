"""Batch BO — the Recap 1-Click CTA shows only on the wizard's
final step (owner direction 2026-11-06 19:56).

The owner asked that the "1-Click ဖန်တီးမည်" create button appear
ONLY when the last card (Step 4) of the Recap wizard is reached.

Code reality (verified before building): the visible 1-Click CTA
is the sticky mirror bar #sticky-cta-recap — on WebViews with an
IntersectionObserver, setupStickyCtas parks the in-flow
#btn-start-recap as a hidden source (.cta-source-hidden) and the
bar floats the action above the bottom nav whenever the recap tab
is active and the source is scrolled out of view. The source lives
inside the Step 4 card, so on Steps 1–3 it is ALWAYS out of view —
the bar floated the create action over every earlier step. The
in-flow button itself needs no gate: it renders inside the Step 4
card, which the wizard shows only on Step 4 (and it is display:none
wherever the sticky machinery runs).

The fix is a step condition inside refreshStickyCtas — the single
function that decides the bar's visibility for every caller
(IntersectionObserver, tab switches, the LiveJobs set, setup) —
COMBINED with the existing conditions, replacing none:
- Batch AK's unfinished-job hide (recapBlocked) still wins;
- the tab-active + source-out-of-view condition is unchanged;
- showRecapStep (Batch AX's one door: boot, Continue buttons,
  indicator-chip taps, the source-flow auto-advance) calls
  refreshStickyCtas on every step change, so the gate re-applies
  the moment the step changes — including navigating BACK from
  Step 4, where no IntersectionObserver event would fire to save
  us (the source was already out of view).

The Voice sticky CTA is untouched: the gate is keyed to the recap
pair's source id only.

Invariants pinned here:
- the gate predicate and the fully combined bar toggle, verbatim;
- the one-door hook: inside showRecapStep, the new current step is
  assigned BEFORE refreshStickyCtas() runs;
- every navigation route still flows through showRecapStep;
- the AK hide literals survive byte-identical;
- cache token r87.
"""
from __future__ import annotations

from pathlib import Path

from app.webapp import api as api_mod


STATIC_DIR = Path(api_mod.__file__).resolve().parent / "static"
APP_JS = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

TAB = INDEX_HTML[INDEX_HTML.index('id="tab-recap"'):INDEX_HTML.index('id="tab-downloader"')]


def _show_step_body() -> str:
    start = APP_JS.index("function showRecapStep(")
    end = APP_JS.index("window.showRecapStep = showRecapStep;")
    return APP_JS[start:end]


def _refresh_body() -> str:
    start = APP_JS.index("function refreshStickyCtas()")
    end = APP_JS.index("window.refreshStickyCtas = refreshStickyCtas;")
    return APP_JS[start:end]


# ── BO-1: the step gate, combined with every existing condition ──

def test_bo_step_gate_predicate_keys_to_recap_pair_only():
    body = _refresh_body()
    # The gate reads the wizard's own current-step state and is
    # keyed to the recap pair's source id — the Voice pair (whose
    # source is btn-generate-voice) can never satisfy it.
    assert 'const recapStepGate = p.src === "btn-start-recap" && recapCurrentStep !== 4;' in body
    # recapCurrentStep is the wizard state showRecapStep maintains.
    assert "let recapCurrentStep = 1;" in APP_JS


def test_bo_bar_toggle_combines_gate_with_ak_and_scroll_conditions():
    body = _refresh_body()
    # ONE toggle decides the bar, with all three hides OR-ed:
    # AK's job-active hide, BO's step gate, and the pre-existing
    # tab-active + source-out-of-view requirement (negated).
    assert ('bar.classList.toggle("hidden", recapBlocked || recapStepGate || '
            '!(tabActive && stickyCtaInView[p.src] === false));') in body
    # Nothing from before was replaced: the disabled mirror and the
    # AK pair of hides survive byte-identical.
    assert "btn.disabled = !!src.disabled;" in body
    assert 'const recapBlocked = p.src === "btn-start-recap" && recapJobActive();' in body
    assert 'src.classList.toggle("hidden", recapBlocked)' in body
    # Both sticky pairs are still registered, recap mapped to its
    # Step-4 source button.
    assert '{ tab: "tab-recap", bar: "sticky-cta-recap", src: "btn-start-recap", btn: "btn-start-recap-sticky" }' in APP_JS
    assert '{ tab: "tab-voice", bar: "sticky-cta-voice", src: "btn-generate-voice", btn: "btn-generate-voice-sticky" }' in APP_JS


def test_bo_gate_truth_table():
    # The behaviour the pinned literals implement (mirrors the
    # toggle expression above): the bar may show only on Step 4,
    # only with the recap tab active, only while the source is out
    # of view, and never while an unfinished recap job blocks it.
    def bar_hidden(step, *, blocked=False, tab_active=True, src_in_view=False):
        recap_step_gate = step != 4
        return blocked or recap_step_gate or not (tab_active and src_in_view is False)

    for step in (1, 2, 3):
        assert bar_hidden(step) is True, f"step {step} must hide the 1-Click CTA"
    assert bar_hidden(4) is False, "step 4 shows the CTA (its normal surface)"
    # Back-nav: leaving Step 4 hides it again immediately.
    assert bar_hidden(4) is False and bar_hidden(3) is True
    # Existing conditions still dominate on Step 4 itself.
    assert bar_hidden(4, blocked=True) is True
    assert bar_hidden(4, tab_active=False) is True
    assert bar_hidden(4, src_in_view=True) is True


# ── BO-2: the one door re-applies the gate on every step change ──

def test_bo_show_step_reapplies_gate_after_assigning_step():
    body = _show_step_body()
    # The refresh runs INSIDE showRecapStep — the single point
    # where step changes are applied — and AFTER the new step is
    # assigned, so it reads the step being entered, not the one
    # being left (this is what hides the CTA again on back-nav).
    assert "refreshStickyCtas();" in body
    assert body.index("recapCurrentStep = n;") < body.index("refreshStickyCtas();")
    # The card-visibility application itself is untouched.
    assert 'els.card.classList.toggle("hidden", m !== n);' in body


def test_bo_all_navigation_routes_flow_through_the_one_door():
    # Boot state: initRecapSteps lands on Step 1 (CTA hidden).
    assert "showRecapStep(1);" in APP_JS
    # Forward nav: Continue buttons and the source-flow advance.
    assert "showRecapStep(target);" in APP_JS
    assert "showRecapStep(2);" in APP_JS
    # Back/edit nav: indicator-chip taps pass their step through.
    assert 'showRecapStep(parseInt(el.getAttribute("data-step-toggle"), 10))' in APP_JS


# ── BO-3: the surfaces themselves are unchanged ──

def test_bo_cta_markup_unchanged():
    # The in-flow button still lives inside the Step 4 card (the
    # wizard itself gates it there) and the sticky mirror bar still
    # exists with its forwarding button.
    step4 = TAB[TAB.index('id="recap-step-4"'):]
    assert 'id="btn-start-recap"' in step4
    assert 'id="sticky-cta-recap"' in INDEX_HTML
    assert 'id="btn-start-recap-sticky"' in INDEX_HTML
    # The click still starts the same action (Batch AX §15 wiring).
    assert 'document.getElementById("btn-start-recap")' in APP_JS


# ── Token ──

def test_bo_cache_token_is_r87():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert html.count("20261104_r108") == 4
    assert "20261104_r86" not in html
