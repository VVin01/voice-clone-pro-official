"""Unit tests for Round 15 (Parts A–G, I, asset stamp, and config parity) — 2026-11-23."""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import shutil
import subprocess
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.base import Base
from app.models.schema import User
from app.services import group_ai_admin as gai
from app.services import key_rotation as kr
from app.services import private_ai as pai
from app.services import voxcpm_engine as vx
from app.services.ledger import get_or_create_user, grant_daily_free_allowance
from app.services.recap_engine import core as recap_core
from app.services.recap_engine import gemini_tts_engine as gtts_mod
from app.services.recap_engine import recap_planner as planner_mod
from app.webapp import api as api_mod
from app.webapp import server as server_mod

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")


def _run_node(script: str, stdin_text: str | None = None) -> dict:
    proc = subprocess.run(
        ["node", "-e", script],
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        cwd=str(ROOT),
    )
    return json.loads(proc.stdout.strip())


def _load_translations() -> dict:
    return _run_node(
        'const fs = require("fs");'
        'const src = fs.readFileSync("app/webapp/static/js/i18n.js", "utf8");'
        'const tr = new Function("localStorage", "window", src + "\\nreturn translations;")'
        '({ getItem: () => "my", setItem: () => {} }, {});'
        "console.log(JSON.stringify(tr));"
    )


# ── Part A: Delete the tier pill from Recap Step 1 notes expander ──────────


def test_part_a_tier_pill_deleted_and_repo_grep_clean():
    deleted_keys = ("recap_tier_vip_text", "recap_tier_free_text")
    this_file = Path(__file__).resolve()
    for p in sorted(ROOT.rglob("*")):
        if any(part in p.parts for part in (".git", "__pycache__", "node_modules", ".pytest_cache")):
            continue
        if not p.is_file() or p.resolve() == this_file:
            continue
        if p.suffix not in (".html", ".js", ".css", ".py", ".json"):
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        for key in deleted_keys:
            assert key not in txt, f"Deleted key {key!r} still referenced in {p}"

    tr = _load_translations()
    for lang in ("my", "en"):
        for key in deleted_keys:
            assert key not in tr[lang], f"{key} still present in translations.{lang}"

    # Step 1 notes expander keeps the rights/privacy note and has NO tier pill
    m = re.search(r'<details class="recap-notes-collapse mt-3">[\s\S]*?</details>', INDEX_HTML)
    assert m is not None, "Missing Step 1 notes expander"
    expander = m.group(0)
    assert 'data-i18n="recap_rights_note"' in expander
    assert "studio-alert-carousel" not in expander
    assert "recap-limit-pill" not in expander
    assert "recap-tier-limit-text" not in expander
    assert "recap_limit_notice" not in expander
    assert "recap-tier-limit-text" not in APP_JS


# ── Part B: Delete the inline status row on Recap Step 1 ──────────────────


def test_part_b_inline_status_strip_removed_and_open_current_repointed():
    assert 'id="recap-inline-status"' not in INDEX_HTML
    assert 'id="recap-inline-status-text"' not in INDEX_HTML
    assert 'getElementById("recap-inline-status")' not in APP_JS
    assert 'getElementById("recap-inline-status-text")' not in APP_JS
    assert 'showStudioInlineStatus("recap"' not in APP_JS

    # Step 1 card's own verification controls & feedback remain untouched
    for el_id in ("btn-fetch-recap-info", "recap-video-info-box", "recap-url-error-card"):
        assert f'id="{el_id}"' in INDEX_HTML

    # openCurrentRecapDetail is preserved and routes to the dedicated sub-view
    assert "window.openCurrentRecapDetail" in APP_JS
    # Active Jobs sheet still keeps the "View details" label key
    tr = _load_translations()
    assert tr["my"]["studio_inline_view"] == "အသေးစိတ် ကြည့်မည်"
    assert tr["en"]["studio_inline_view"] == "View details"


# ── Part C: Dedicated Recap Job Progress sub-view with real stage ─────────


def test_part_c_dedicated_subview_markup_and_single_language_copy():
    # Step 1 / #tab-recap contains NO inline progress box
    recap_start = INDEX_HTML.index('<section id="tab-recap" class="tab-pane">')
    recap_end = INDEX_HTML.index("</section>", recap_start)
    recap_pane = INDEX_HTML[recap_start:recap_end]
    assert 'id="recap-progress-box"' not in recap_pane

    # Dedicated #tab-recap-job pane hosts the progress content
    job_start = INDEX_HTML.index('<section id="tab-recap-job" class="tab-pane">')
    job_end = INDEX_HTML.index("</section>", job_start)
    job_pane = INDEX_HTML[job_start:job_end]
    for el_id in (
        "btn-recap-job-back",
        "recap-progress-box",
        "recap-stage-stepper",
        "recap-progress-fill",
        "recap-status-msg",
        "recap-step-text",
        "btn-recap-cancel",
    ):
        assert f'id="{el_id}"' in job_pane, f"Missing #{el_id} inside #tab-recap-job"
    assert 'data-i18n="telegram_leave_hint"' in job_pane
    assert "0/4" not in job_pane

    # Global Rule 6: single-language MY headings/labels (no Latin letters)
    tr = _load_translations()
    for key in ("recap_job_back_btn", "recap_job_view_title"):
        assert re.search(r"[A-Za-z]", tr["my"][key]) is None, f"MY {key} mixes Latin: {tr['my'][key]!r}"
        assert tr["en"][key].strip()


def test_part_c_subview_navigation_and_truthful_stage_rendering_node():
    """Verify via Node:
    1) Active Jobs sheet open -> #tab-recap-job activates, return tab recorded.
    2) Mid-render live-job payload (progress_stage='overlay', stage_index=0)
       lights chip 4 (done on 0..2, active on 3) and matches Active Jobs sheet text.
    3) Freshly queued payload shows honest queued copy, 0 active chips, NEVER '0/4'.
    4) Back returns to the exact tab where the sheet was opened from without cancelling.
    """
    tr = _load_translations()
    out = _run_node(
        r"""
const fs = require("fs");
const vm = require("vm");
const appSrc = fs.readFileSync("app/webapp/static/js/app.js", "utf8");
const tr = JSON.parse(fs.readFileSync(0, "utf8"));

function sliceBetween(startMark, endMark) {
  const s = appSrc.indexOf(startMark);
  const e = appSrc.indexOf(endMark, s);
  if (s < 0 || e < 0) throw new Error("Missing slice: " + startMark + " -> " + endMark);
  return appSrc.slice(s, e);
}

const stepperCode = sliceBetween("function renderStageStepper(", "// ── Sticky create CTAs");
const liveStageCode = sliceBetween("function liveJobStageText(", "let liveJobsPillDismissedSig");
const paintFillCode = sliceBetween("function paintProgressBarFill(", "function syncRecapStateFromLiveJobs");
const detailCode = sliceBetween("state.voiceDetailOpen = false;", "function showLiveSubmitLimit");
const renderFormCode = sliceBetween("let recapFormJobId = null;", "function startRecapJobPolling");

function mkEl(id, initialClasses = []) {
  const set = new Set(initialClasses);
  return {
    id,
    innerText: "",
    innerHTML: "",
    dataset: {},
    style: {},
    disabled: false,
    classList: {
      add(...c) { c.forEach(x => set.add(x)); },
      remove(...c) { c.forEach(x => set.delete(x)); },
      toggle(c, force) { if (force === undefined) { if (set.has(c)) set.delete(c); else set.add(c); } else if (force) set.add(c); else set.delete(c); },
      contains(c) { return set.has(c); },
    },
    scrollIntoView() {},
  };
}

const els = {
  "tab-home": mkEl("tab-home", ["tab-pane", "active"]),
  "tab-recap": mkEl("tab-recap", ["tab-pane"]),
  "tab-recap-job": mkEl("tab-recap-job", ["tab-pane"]),
  "recap-progress-box": mkEl("recap-progress-box", ["hidden"]),
  "recap-job-title": mkEl("recap-job-title", ["hidden"]),
  "recap-stage-stepper": mkEl("recap-stage-stepper"),
  "recap-progress-fill": mkEl("recap-progress-fill"),
  "recap-status-msg": mkEl("recap-status-msg"),
  "recap-step-text": mkEl("recap-step-text"),
  "btn-recap-cancel": mkEl("btn-recap-cancel"),
};

let currentTab = "tab-home";
const ctx = {
  window: {},
  currentLang: "my",
  state: { recapJobId: null, recapPollTimer: 99, activeJobId: null, pollTimer: null },
  t: (k) => (tr.my[k] !== undefined ? tr.my[k] : k),
  tf: (k, params) => {
    let s = tr.my[k] || k;
    for (const [pk, pv] of Object.entries(params || {})) s = s.replace("{" + pk + "}", String(pv));
    return s;
  },
  liveJobsIsTerminal: (st) => ["SUCCESS", "FAILED", "CANCELLED"].includes(st),
  formatEtaRangeText: () => "",
  formatRecapQuoteDuration: (s) => `${s}s`,
  syncTelegramBackButton: () => {},
  startRecapJobPolling: () => {},
  LiveJobs: {
    jobs: new Map(),
    _subs: [],
    activeJobs() { return [...this.jobs.values()].filter(j => !["SUCCESS", "FAILED", "CANCELLED"].includes(j.status)); },
    subscribe(fn) { this._subs.push(fn); },
  },
  switchTab(id) {
    Object.values(els).forEach(el => { if (el.classList.contains("tab-pane")) el.classList.remove("active"); });
    if (els[id]) els[id].classList.add("active");
    currentTab = id;
  },
  document: {
    getElementById: (id) => els[id] || null,
    querySelector: (sel) => sel === ".tab-pane.active" ? els[currentTab] : null,
  },
};

vm.runInNewContext(
  stepperCode + "\n" + liveStageCode + "\n" + paintFillCode + "\n" + renderFormCode + "\n" + detailCode,
  ctx
);

// 1) Mid-render job in LiveJobs (progress_stage='overlay', stage_index=0 like live incident)
const runningJob = {
  id: "rc_live_1",
  kind: "recap",
  status: "RUNNING",
  stage_index: 0,
  stage_total: 4,
  progress_stage: "overlay",
  progress_percent: 82,
  elapsed_seconds: 95,
  title: "Demo Movie",
};
ctx.LiveJobs.jobs.set("rc_live_1", runningJob);
const sheetStageText = ctx.liveJobStageText(runningJob);

ctx.window.openCurrentRecapDetail("rc_live_1");
const openedTab = currentTab;
const runningStatusMsg = els["recap-status-msg"].innerText;
const runningStepText = els["recap-step-text"].innerText;
const runningStepperHtml = els["recap-stage-stepper"].innerHTML;
const runningFillWidth = els["recap-progress-fill"].style.width;

// 2) Return via Back -> returns to tab-home without killing poller or detaching job
ctx.window.backToStudio("recap");
const returnedTab = currentTab;
const pollStillActive = ctx.state.recapPollTimer === 99;
const jobStillTracked = ctx.LiveJobs.jobs.has("rc_live_1");

// 3) Freshly queued job (no stage data yet) -> honest queued state, never "0/4"
const queuedJob = {
  id: "rc_queued_2",
  kind: "recap",
  status: "QUEUED",
  stage_index: 0,
  stage_total: 4,
  progress_stage: null,
  progress_percent: 0,
  title: "Fresh Clip",
};
ctx.LiveJobs.jobs.set("rc_queued_2", queuedJob);
ctx.window.openCurrentRecapDetail("rc_queued_2");
const queuedStatusMsg = els["recap-status-msg"].innerText;
const queuedStepText = els["recap-step-text"].innerText;
const queuedStepperHtml = els["recap-stage-stepper"].innerHTML;

console.log(JSON.stringify({
  openedTab,
  sheetStageText,
  runningStatusMsg,
  runningStepText,
  runningStepperHtml,
  runningFillWidth,
  returnedTab,
  pollStillActive,
  jobStillTracked,
  queuedStatusMsg,
  queuedStepText,
  queuedStepperHtml,
}));
""",
        stdin_text=json.dumps(tr),
    )

    assert out["openedTab"] == "tab-recap-job"
    assert out["runningStatusMsg"] == out["sheetStageText"]
    assert "ဗီဒီယိုနှင့် စာတန်းထိုး" in out["runningStatusMsg"]
    assert "4/4" in out["runningStepText"]
    assert "0/4" not in out["runningStepText"]
    assert out["runningStepperHtml"].count("stage-chip done") == 3
    assert out["runningStepperHtml"].count("stage-chip active") == 1
    assert "④ ဗီဒီယို" in out["runningStepperHtml"]
    assert out["runningFillWidth"] == "82%"

    assert out["returnedTab"] == "tab-home"
    assert out["pollStillActive"] is True
    assert out["jobStillTracked"] is True

    assert out["queuedStatusMsg"] == tr["my"]["live_job_queued"]
    assert "0/4" not in out["queuedStepText"]
    assert "stage-chip active" not in out["queuedStepperHtml"]
    assert "stage-chip done" not in out["queuedStepperHtml"]
    assert out["queuedStepperHtml"].count("stage-chip upcoming") == 4


# ── Part D: DB connection-pool rebalance & burst check ───────────────────


@pytest.mark.asyncio
async def test_part_d_pool_defaults_budget_and_concurrent_user_me_burst(tmp_path):
    assert settings.db_pool_size == 5
    assert settings.db_max_overflow == 2
    assert settings.db_pool_timeout == 30
    assert settings.db_pool_recycle == 1800

    # Per-process repo default cap <= 7; combined default <= 14; deploy split 7 + 5 = 12 <= 15 - 3
    assert settings.db_pool_size + settings.db_max_overflow == 7
    web_max = 6 + 1
    worker_max = 4 + 1
    supabase_ceiling = 15
    assert web_max + worker_max == 12 <= supabase_ceiling - 3

    # File-backed engine with the exact new pool defaults (5 / 2) handles 10 concurrent /api/user/me
    db_file = tmp_path / "r15_pool_burst.db"
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{db_file.as_posix()}",
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
        future=True,
    )
    try:
        assert eng.pool.size() == 5
        assert eng.pool._max_overflow == 2
        assert eng.pool._pre_ping is True

        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        SessionMaker = async_sessionmaker(
            bind=eng, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        async with SessionMaker() as s:
            u, _ = await get_or_create_user(s, telegram_id=951501, username="r15burst", first_name="Burst")
            await grant_daily_free_allowance(s, u.id)
            await s.commit()

        # Warm once, then measure 10 concurrent /api/user/me calls
        async with SessionMaker() as s:
            u = await s.get(User, 951501)
            baseline_payload = await api_mod.get_user_profile(u, s)

        latencies: list[float] = []

        async def _one_call():
            t0 = time.perf_counter()
            async with SessionMaker() as s:
                u_obj = await s.get(User, 951501)
                res = await api_mod.get_user_profile(u_obj, s)
            latencies.append(time.perf_counter() - t0)
            return res

        results = await asyncio.gather(*[_one_call() for _ in range(10)])
        assert len(results) == 10
        assert all(r == baseline_payload for r in results)
        assert max(latencies) < 4.0, f"Burst max latency {max(latencies):.3f}s >= 4.0s"
    finally:
        await eng.dispose()


# ── Part E: Groq routing (Group/Private Groq-primary, Recap Groq-fallback) ─


class _FakeHttpResp:
    def __init__(self, status_code: int, body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text or json.dumps(self._body)

    def json(self):
        return self._body


@pytest.mark.asyncio
async def test_part_e_group_and_private_ai_groq_primary_and_gemini_fallback(monkeypatch):
    kr.health_for(kr.GROQ_TEXT_PROVIDER).reset()
    kr.health_for("gemini").reset()
    monkeypatch.setenv("GROQ_API_KEYS", "gsk_acc1,gsk_acc2")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gem_key_1")

    # (a1) Gemini failing -> Group AI answers via Groq primary (and parses MODERATION)
    gemini_called = []

    def _dead_gemini_factory(_key):
        gemini_called.append(_key)
        raise RuntimeError("gemini dead")

    groq_models_seen = []

    def _ok_groq_post(url, headers=None, json=None, timeout=None):
        groq_models_seen.append((url, json["model"]))
        return _FakeHttpResp(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": "MODERATION: category=none confidence=0.0\nREPLY: စမ်းသပ်ဖြေကြားချက်ပါရှင့်။"
                        }
                    }
                ]
            },
        )

    res = await gai.ask_group_admin_ai(
        text="ဘယ်လိုသုံးရလဲ",
        author_name="Aung",
        client_factory=_dead_gemini_factory,
        groq_requester=_ok_groq_post,
    )
    assert res == (gai.DECISION_REPLY, "စမ်းသပ်ဖြေကြားချက်ပါရှင့်။")
    assert res.moderation == {"category": "none", "confidence": 0.0}
    assert groq_models_seen == [(kr.GROQ_TEXT_ENDPOINT, "qwen/qwen3.8-27b")]
    assert gemini_called == []  # Gemini never touched when Groq succeeds

    # Private AI shares the exact Groq-primary path via gai._ask_ai_sync
    monkeypatch.setattr(gai, "_ask_groq_sync", lambda prompt, requester=None: "REPLY: Private Groq အဖြေပါ။")
    monkeypatch.setattr(gai, "_ask_gemini_sync", lambda prompt, client_factory=None: (_ for _ in ()).throw(RuntimeError("gem dead")))
    priv_dec, priv_text = await pai.ask_private_ai(text="စျေးနှုန်းသိချင်ပါတယ်", author_name="Su")
    assert (priv_dec, priv_text) == (gai.DECISION_REPLY, "Private Groq အဖြေပါ။")

    # (a2) Groq failing -> Group AI falls back to Gemini seamlessly
    monkeypatch.undo()
    kr.health_for(kr.GROQ_TEXT_PROVIDER).reset()
    kr.health_for("gemini").reset()
    monkeypatch.setenv("GROQ_API_KEYS", "gsk_fail1,gsk_fail2")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(gai, "collect_gemini_key_pool", lambda env=None: [("GEMINI_API_KEY", "gem_ok_1")])
    monkeypatch.setattr(gai, "gemini_model_chain", lambda: ["gemini-3.6-flash"])

    def _failing_groq_post(url, headers=None, json=None, timeout=None):
        return _FakeHttpResp(503, text="service unavailable")

    class _OkModels:
        def generate_content(self, model=None, contents=None, config=None):
            return SimpleNamespace(text="REPLY: Gemini အရန်အဖြေပါ။")

    res_fb = await gai.ask_group_admin_ai(
        text="ဘယ်လိုသုံးရလဲ",
        client_factory=lambda _k: SimpleNamespace(models=_OkModels()),
        groq_requester=_failing_groq_post,
    )
    assert res_fb == (gai.DECISION_REPLY, "Gemini အရန်အဖြေပါ။")

    # (b) Both Groq and Gemini fail -> silence (DECISION_AI_FAILED)
    class _DeadModels:
        def generate_content(self, model=None, contents=None, config=None):
            raise RuntimeError("gemini down")

    res_both = await gai.ask_group_admin_ai(
        text="ဘယ်လိုသုံးရလဲ",
        client_factory=lambda _k: SimpleNamespace(models=_DeadModels()),
        groq_requester=_failing_groq_post,
    )
    assert res_both == (gai.DECISION_AI_FAILED, None)


def test_part_e_groq_key_rotation_dedup_and_dead_key_skipped(monkeypatch):
    health = kr.health_for(kr.GROQ_TEXT_PROVIDER)
    health.reset()
    # 5 configured with 1 duplicate -> 4 unique keys
    env = {
        "GROQ_API_KEYS": "gsk_dead,gsk_k2,gsk_k3,gsk_k2",
        "GROQ_API_KEY": "gsk_k4",
    }
    pool = kr.collect_groq_key_pool(env)
    assert [k for _, k in pool] == ["gsk_dead", "gsk_k2", "gsk_k3", "gsk_k4"]

    used_keys: list[str] = []

    def _fake_post(url, headers=None, json=None, timeout=None):
        key = headers["Authorization"].removeprefix("Bearer ")
        used_keys.append(key)
        if key == "gsk_dead":
            return _FakeHttpResp(401, text="invalid api key")
        return _FakeHttpResp(200, {"choices": [{"message": {"content": f"ok:{key}"}}]})

    # Call 1 hits gsk_dead (401 -> cooled), fails over to gsk_k2
    out1 = kr.ask_groq_text_sync("hello 1", requester=_fake_post, env=env)
    assert out1 == "ok:gsk_k2"
    assert used_keys == ["gsk_dead", "gsk_k2"]
    assert health.in_cooldown("gsk_dead") is True

    # Calls 2 & 3 skip gsk_dead completely and rotate across healthy keys (k3, k4)
    used_keys.clear()
    out2 = kr.ask_groq_text_sync("hello 2", requester=_fake_post, env=env)
    out3 = kr.ask_groq_text_sync("hello 3", requester=_fake_post, env=env)
    assert "gsk_dead" not in used_keys
    assert len(set(used_keys)) == 2
    assert out2.startswith("ok:gsk_") and out3.startswith("ok:gsk_")


def test_part_e_recap_planner_gemini_primary_groq_fallback_and_oversize_skip(monkeypatch):
    kr.health_for(kr.GROQ_TEXT_PROVIDER).reset()
    monkeypatch.setenv("GROQ_API_KEYS", "gsk_recap_1")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    groq_calls: list[str] = []

    def _fake_groq(prompt, *, log=None, requester=None):
        groq_calls.append(prompt)
        return planner_mod._try_groq_json_fallback.__wrapped__(prompt, log=log, requester=requester)

    # Spy on kr.ask_groq_text_sync directly
    real_ask_groq = kr.ask_groq_text_sync

    def _spy_groq(prompt, **kw):
        groq_calls.append(prompt)
        return real_ask_groq(
            prompt,
            requester=lambda url, headers=None, json=None, timeout=None: _FakeHttpResp(
                200, {"choices": [{"message": {"content": '{"items": [{"id": 1, "translation": "စမ်းသပ်"}]}'}}]}
            ),
            **{k: v for k, v in kw.items() if k != "requester"},
        )

    monkeypatch.setattr(kr, "ask_groq_text_sync", _spy_groq)

    def _make_ai(succeed: bool):
        class _Models:
            def generate_content(self, model=None, contents=None, config=None):
                if succeed:
                    return SimpleNamespace(text='{"items": [{"id": 1, "translation": "Gemini အဖြေ"}]}')
                raise RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded")

        ai = SimpleNamespace(
            client=SimpleNamespace(models=_Models()),
            text_model="gemini-3.6-flash",
            api_key="gem_k1",
            _enable_proxy=lambda: None,
            _disable_proxy=lambda: None,
            _recap_active_key_label="GEMINI_API_KEY",
            _recap_active_model="gemini-3.6-flash",
            _recap_key_pool_size=1,
            _recap_model_pool_size=1,
            _recap_rotate_gemini_key=lambda: False,
            _recap_advance_after_key_pool=lambda: False,
        )
        return ai

    # (e) Gemini succeeds -> ZERO Groq calls made
    groq_calls.clear()
    out_gem = planner_mod._gemini_json(_make_ai(succeed=True), "short prompt", retries=1)
    assert out_gem == {"items": [{"id": 1, "translation": "Gemini အဖြေ"}]}
    assert groq_calls == []

    # (d1) Gemini chain exhausted on normal prompt (~5K tokens) -> Groq fallback recovers
    groq_calls.clear()
    out_fb = planner_mod._gemini_json(_make_ai(succeed=False), "translate these lines " * 500, retries=1)
    assert out_fb == {"items": [{"id": 1, "translation": "စမ်းသပ်"}]}
    assert len(groq_calls) == 1

    # (d2) Gemini chain exhausted on oversize >12K-token prompt -> Groq leg skipped gracefully (0 calls)
    groq_calls.clear()
    oversize_prompt = "A" * 52000  # ~13,000 tokens > 12,000 TPM cap
    assert kr.estimate_prompt_tokens(oversize_prompt) > planner_mod.GROQ_RECAP_MAX_PROMPT_TOKENS
    with pytest.raises(planner_mod.GeminiKeyPoolExhaustedError):
        planner_mod._gemini_json(_make_ai(succeed=False), oversize_prompt, retries=1)
    assert groq_calls == []


# ── Part F: HF single pool — retire paid-only reserve ────────────────────


def _write_wav(path: Path, seconds: float = 0.25, rate: int = 24000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def test_part_f_hf_single_pool_merges_reserve_and_serves_free_tier(tmp_path, monkeypatch):
    # (b) Pool merges all 31 HF_TOKENS + 1 HF_RESERVE_TOKENS = 32 unique, deduplicated
    base_31 = [f"hf_pool_{i:02d}" for i in range(1, 32)]
    reserve_tok = "hf_reserve_pro_01"
    monkeypatch.setenv("HF_TOKENS", ",".join(base_31 + [base_31[0]]))
    monkeypatch.setenv("HF_RESERVE_TOKENS", f"{reserve_tok},{base_31[1]}")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
    monkeypatch.setattr(settings, "hf_token", "")
    monkeypatch.setattr(settings, "hf_tokens", "")

    pool = vx.get_available_hf_tokens()
    assert len(pool) == 32
    assert len(set(pool)) == 32
    assert reserve_tok in pool
    assert all(tok in pool for tok in base_31)

    # (a) FREE-tier synthesis CAN be served by the former reserve token
    monkeypatch.setattr(vx.VoxCPMEngine, "_token_balances", {})
    monkeypatch.setattr(vx.VoxCPMEngine, "_token_cooldowns", {})
    monkeypatch.setattr(vx.VoxCPMEngine, "_last_admission_price", None)
    monkeypatch.setattr(vx.VoxCPMEngine, "_token_rr_counter", 0)
    monkeypatch.setattr(vx.VoxCPMEngine, "_token_bind_rr_counters", {})
    monkeypatch.setattr(vx.VoxCPMEngine, "_space_admission_prices", {})
    monkeypatch.setenv("HF_TOKENS", "hf_shared_empty")
    monkeypatch.setenv("HF_RESERVE_TOKENS", reserve_tok)

    wav_src = _write_wav(tmp_path / "synth_out.wav")
    tried: list[str] = []

    class _FakeClient:
        def __init__(self, tok: str):
            self.tok = tok

        def submit(self, **_kw):
            tried.append(self.tok)
            if self.tok != reserve_tok:
                raise RuntimeError("You have exceeded your free ZeroGPU quota (94s requested vs. 0s left)")
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(wav_src))

    engine = vx.VoxCPMEngine(work_dir=tmp_path / "work")
    engine.spaces = ["spaceA"]
    assert reserve_tok in engine.hf_tokens
    monkeypatch.setattr(
        vx.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, space, hf_token=None: _FakeClient(hf_token)),
    )
    out = engine.synthesize(
        text="မင်္ဂလာပါ စမ်းသပ်",
        output_wav=tmp_path / "free_result.wav",
        allow_fallback=False,
        tier="free",
    )
    assert out.is_file()
    assert reserve_tok in tried

    # (c) Grep-level assertion: no reserve-gating code path remains in voxcpm_engine.py
    vx_src = (ROOT / "app/services/voxcpm_engine.py").read_text(encoding="utf-8")
    for forbidden in (
        "_dp_reserve",
        "_reserve_token_set",
        "_designated_reserve_tokens",
        "VoxCPM reserve: held",
    ):
        assert forbidden not in vx_src, f"Reserve-gating artifact {forbidden!r} still in voxcpm_engine.py"


# ── Part G: Gemini 23-key text pool ──────────────────────────────────────


def test_part_g_gemini_23_key_collection_dedup_and_rotation_beyond_6(monkeypatch):
    # (a) All 23 env names set -> 23 unique keys
    env_23: dict[str, str] = {"GEMINI_API_KEY": "gem_main_0"}
    for i in range(1, 6):
        env_23[f"GEMINI_API_KEY_{i}"] = f"gem_main_{i}"
    for i in range(1, 18):
        env_23[f"GEMINI_IMAGE_PROJECT_{i}_KEY"] = f"gem_img_{i}"

    pool_23 = kr.collect_gemini_key_pool(env_23)
    assert len(pool_23) == 23
    assert len({k for _, k in pool_23}) == 23
    assert set(gtts_mod.collect_gemini_keys(env_23)) == {k for _, k in pool_23}

    # Also verify gemini_tts_engine standalone fallback (without key_rotation) unions all 23
    monkeypatch.setattr(gtts_mod, "_key_rotation", None)
    standalone_23 = gtts_mod.collect_gemini_keys(env_23)
    assert len(standalone_23) == 23
    monkeypatch.undo()

    # (b) Duplicates collapse cleanly
    env_dup = dict(env_23)
    env_dup["GEMINI_IMAGE_PROJECT_1_KEY"] = "gem_main_0"
    env_dup["GEMINI_IMAGE_PROJECT_2_KEY"] = "gem_main_1"
    pool_dup = kr.collect_gemini_key_pool(env_dup)
    assert len(pool_dup) == 21
    assert len({k for _, k in pool_dup}) == 21

    # (c) Only original 6 set -> unchanged at 6, no crash on missing image-project vars
    env_6 = {"GEMINI_API_KEY": "gem_main_0", **{f"GEMINI_API_KEY_{i}": f"gem_main_{i}" for i in range(1, 6)}}
    pool_6 = kr.collect_gemini_key_pool(env_6)
    assert len(pool_6) == 6

    # (d) Mocked Group-AI Gemini chain rotates beyond the first 6 keys across the 23-key pool
    kr.health_for("gemini").reset()
    monkeypatch.setattr(gai, "collect_gemini_key_pool", lambda env=None: pool_23)
    monkeypatch.setattr(gai, "gemini_model_chain", lambda: ["gemini-3.6-flash"])

    served_keys: list[str] = []

    def _factory(api_key: str):
        class _Models:
            def generate_content(self, model=None, contents=None, config=None):
                served_keys.append(api_key)
                return SimpleNamespace(text="REPLY: ok")

        return SimpleNamespace(models=_Models())

    for _ in range(10):
        assert gai._ask_gemini_sync("test prompt", client_factory=_factory) == "REPLY: ok"

    assert len(set(served_keys)) == 10, f"Expected rotation across >=10 distinct keys, got {served_keys}"


# ── Part I: Never upscale recap output ───────────────────────────────────


def _make_synthetic_video(path: Path, width: int, height: int, seconds: float = 1.0) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    proc = subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=10",
            "-t", str(seconds),
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return path


def test_part_i_never_upscale_geometry_all_acceptance_cases():
    # (a) 720x1280 source + VIP + 9:16 -> 720x1280 (plain and with locked 12% CinemaScope crop)
    lay_720 = recap_core.frame_layout("9:16", "blur", 720, 1280, max_short_side=1080)
    assert (lay_720["cw"], lay_720["ch"]) == (720, 1280)
    assert (lay_720["pw"], lay_720["ph"]) == (720, 1280)

    lay_720_crop = recap_core.frame_layout(
        "9:16", "blur", 720, 1280, crop_top=0.12, crop_bottom=0.12, max_short_side=1080
    )
    assert (lay_720_crop["cw"], lay_720_crop["ch"]) == (720, 1280)
    assert lay_720_crop["pw"] == 720 and lay_720_crop["ph"] == lay_720_crop["c_sh"] == 972

    # (b) 1920x1080 (and 1080x1920) + VIP + 9:16 -> 1080x1920, byte-identical to base build
    lay_1080_ls = recap_core.frame_layout("9:16", "blur", 1920, 1080, max_short_side=1080)
    assert (lay_1080_ls["cw"], lay_1080_ls["ch"]) == (1080, 1920)
    assert (lay_1080_ls["pw"], lay_1080_ls["ph"], lay_1080_ls["ox"], lay_1080_ls["oy"]) == (1080, 608, 0, 656)

    lay_1080_pt = recap_core.frame_layout(
        "9:16", "blur", 1080, 1920, crop_top=0.12, crop_bottom=0.12, max_short_side=1080
    )
    assert (lay_1080_pt["cw"], lay_1080_pt["ch"]) == (1080, 1920)
    assert (lay_1080_pt["pw"], lay_1080_pt["ph"], lay_1080_pt["ox"], lay_1080_pt["oy"]) == (1080, 1460, 0, 230)

    # (c) 854x480 source + VIP -> short side 480 (16:9 and Original)
    lay_480_169 = recap_core.frame_layout("16:9", "blur", 854, 480, max_short_side=1080)
    assert (lay_480_169["cw"], lay_480_169["ch"]) == (854, 480)
    assert min(lay_480_169["cw"], lay_480_169["ch"]) == 480

    lay_480_orig = recap_core.frame_layout("Original", "blur", 854, 480, max_short_side=1080)
    assert (lay_480_orig["cw"], lay_480_orig["ch"]) == (854, 480)

    lay_480_pt = recap_core.frame_layout("9:16", "blur", 480, 854, max_short_side=1080)
    assert (lay_480_pt["cw"], lay_480_pt["ch"]) == (480, 854)

    # (d) Landscape source -> 9:16 blur-fill: foreground never exceeds 1.0x and canvas follows min()
    lay_480_blur916 = recap_core.frame_layout("9:16", "blur", 854, 480, max_short_side=1080)
    assert (lay_480_blur916["cw"], lay_480_blur916["ch"]) == (854, 1518)
    assert (lay_480_blur916["pw"], lay_480_blur916["ph"]) == (854, 480)
    assert lay_480_blur916["pw"] <= lay_480_blur916["c_sw"]
    assert lay_480_blur916["ph"] <= lay_480_blur916["c_sh"]

    lay_360_blur916 = recap_core.frame_layout("9:16", "blur", 640, 360, max_short_side=1080)
    assert (lay_360_blur916["cw"], lay_360_blur916["ch"]) == (640, 1138)
    assert (lay_360_blur916["pw"], lay_360_blur916["ph"]) == (640, 360)

    # (e) Subtitle geometry scales proportionally on the reduced 720x1280 canvas
    font_1080 = recap_core.subtitle_font_px(1080, 1920, 16, 1, lay_1080_pt)
    font_720 = recap_core.subtitle_font_px(720, 1280, 16, 1, lay_720_crop)
    assert font_1080 == 54 and font_720 == 36
    assert abs((font_720 / 720.0) - (font_1080 / 1080.0)) < 1e-6

    # (f) Free-tier (max_short_side=720) from 1080p source still outputs 720 short side;
    #     sub-720 source stays at native size
    lay_free_1080p = recap_core.frame_layout("9:16", "blur", 1080, 1920, max_short_side=720)
    assert (lay_free_1080p["cw"], lay_free_1080p["ch"]) == (720, 1280)
    lay_free_1080ls = recap_core.frame_layout("16:9", "blur", 1920, 1080, max_short_side=720)
    assert (lay_free_1080ls["cw"], lay_free_1080ls["ch"]) == (1280, 720)
    lay_free_360 = recap_core.frame_layout("16:9", "blur", 640, 360, max_short_side=720)
    assert (lay_free_360["cw"], lay_free_360["ch"]) == (640, 360)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe required",
)
def test_part_i_never_upscale_real_ffmpeg_render_and_subtitle_burn(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    from PIL import Image

    # 1) 720x1280 portrait -> VIP 9:16 blur + 12% crop -> real rendered video is 720x1280
    src_720 = _make_synthetic_video(tmp_path / "src_720x1280.mp4", 720, 1280)
    lay_720 = recap_core.frame_layout(
        "9:16", "blur", 720, 1280, crop_top=0.12, crop_bottom=0.12, max_short_side=1080
    )
    out_720 = tmp_path / "out_720.mp4"
    recap_core.render_video_part(
        ffmpeg=ffmpeg,
        source=src_720,
        output=out_720,
        seg={"video_start": 0.0, "video_end": 0.8},
        target_duration=0.8,
        encoder="libx264",
        mirror_video=True,
        zoom_factor=1.10,
        layout=lay_720,
    )
    assert recap_core.probe_video_size(ffprobe, out_720) == (720, 1280)

    # 2) 854x480 landscape -> VIP 9:16 blur -> real rendered video is 854x1518 (not 1080x1920)
    src_480 = _make_synthetic_video(tmp_path / "src_854x480.mp4", 854, 480)
    lay_480 = recap_core.frame_layout("9:16", "blur", 854, 480, max_short_side=1080)
    out_480 = tmp_path / "out_480_916.mp4"
    recap_core.render_video_part(
        ffmpeg=ffmpeg,
        source=src_480,
        output=out_480,
        seg={"video_start": 0.0, "video_end": 0.8},
        target_duration=0.8,
        encoder="libx264",
        mirror_video=True,
        zoom_factor=1.10,
        layout=lay_480,
    )
    assert recap_core.probe_video_size(ffprobe, out_480) == (854, 1518)

    # 3) Subtitle burn on reduced 720x1280 canvas renders cleanly at 720x1280
    prev_720 = recap_core.extract_preview_with_overlays(
        ffmpeg=ffmpeg,
        source_video=src_720,
        output_image=tmp_path / "prev_720.jpg",
        timestamp=0.3,
        overlays={"burn_subtitles": True, "subtitles_font_size": 16, "sub_style": "cinematic", "sub_lines": 1},
        output_ratio="9:16",
        frame_fill="blur",
        crop_top=0.12,
        crop_bottom=0.12,
        ffprobe=ffprobe,
        max_short_side=1080,
    )
    with Image.open(prev_720) as im:
        assert im.size == (720, 1280)

    # 4) Free-tier (max_short_side=720) on 1080x1920 source outputs 720x1280
    src_1080 = _make_synthetic_video(tmp_path / "src_1080x1920.mp4", 1080, 1920)
    lay_free = recap_core.frame_layout("9:16", "blur", 1080, 1920, max_short_side=720)
    out_free = tmp_path / "out_free_720.mp4"
    recap_core.render_video_part(
        ffmpeg=ffmpeg,
        source=src_1080,
        output=out_free,
        seg={"video_start": 0.0, "video_end": 0.8},
        target_duration=0.8,
        encoder="libx264",
        mirror_video=True,
        zoom_factor=1.10,
        layout=lay_free,
    )
    assert recap_core.probe_video_size(ffprobe, out_free) == (720, 1280)


# ── Global Rule 4 & Config-build parity ──────────────────────────────────


def test_global_rule_4_asset_version_stamp_round15():
    assert "?v=20261123_round14" not in INDEX_HTML
    assert INDEX_HTML.count("?v=20261125_admin_r1") == 5
    serve_src = inspect.getsource(server_mod.serve_index)
    assert "?v=20261125_admin_r1" in serve_src
    assert "?v=20261123_round14" not in serve_src


def test_recap_config_build_byte_identical_to_base_3dda9a3():
    base_js = subprocess.check_output(
        ["git", "show", "3dda9a3:app/webapp/static/js/app.js"],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
    )

    def _run_scenarios(js_src: str) -> dict:
        return _run_node(
            r"""
const fs = require("fs");
const src = fs.readFileSync(0, "utf8");
const start = src.indexOf("function buildRecapConfig(url)");
const end = src.indexOf('if (typeof window !== "undefined") window.buildRecapConfig', start);
if (start < 0 || end < 0) throw new Error("buildRecapConfig bounds not found");
const body = src.slice(start, end) + "\nreturn buildRecapConfig(url);";
const RECAP_LOCKED_FILL = "blur";
const RECAP_LOCKED_MIRROR = true;
const RECAP_LOCKED_ZOOM = 1.10;
const RECAP_LOCKED_PROTECTION = "strong";
const recapLockedCropForRatio = (r) => r === "9:16"
  ? { crop_top: 12, crop_bottom: 12, crop_left: 0, crop_right: 0 }
  : { crop_top: 0, crop_bottom: 0, crop_left: 0, crop_right: 0 };
const normalizeEdgeVoiceKey = (v) => String(v || "thiha").trim().toLowerCase();
const ddConfigExtras = () => ({});
const canUseRecapCloneEngine = () => true;
const canUseRecapRenderQuality = () => false;
const run = (overrides) => {
  const state = Object.assign({
    recapVideoTitle: "Sample", recapVoice: "komin", recapStoryStyle: "pov",
    recapRatio: "9:16", recapSpeed: "+12%", recapBlurSubs: true,
    recapBlurBoxTouched: false, recapBlurBoxes: [], recapBlurAutoDetect: true,
    recapBurnSubs: true, recapSubPosBand: "bottom", recapSubMargin: 5,
    recapSubStyle: "cinematic", recapFontSize: 13, recapSubFont: "padauk",
    recapLines: 1, recapLogo: true, recapLogoPos: "top-right",
    recapLogoScale: 0.16, recapLogoOpacity: 0.70, recapSourceDuration: 120,
    recapScriptReview: false, recapRenderQuality: "max",
    recapTtsEngine: "voxcpm", recapGeminiVoice: "", recapEdgeVoice: "ava",
    recapOwnVoiceSelected: false, recapOwnVoiceStatus: "idle", recapOwnVoiceRefId: null,
  }, overrides);
  const dom = {
    "slider-blur-y": { value: "85" }, "slider-blur-h": { value: "7" },
    "slider-blur-x": { value: "4" }, "slider-blur-w": { value: "92" },
    "slider-blur-strength": { value: "18" }, "slider-sub-y": { value: "5" },
    "slider-sub-x": { value: "50" }, "slider-sub-w": { value: "70" },
    "recap-persona-select": { value: state.recapVoice },
    "recap-edge-voice-select": { value: state.recapEdgeVoice },
    "recap-gemini-voice-select": { value: state.recapGeminiVoice },
    "recap-script-review-toggle": { checked: !!state.recapScriptReview },
  };
  const fn = new Function(
    "url", "state", "document", "RECAP_LOCKED_FILL", "RECAP_LOCKED_MIRROR",
    "RECAP_LOCKED_ZOOM", "RECAP_LOCKED_PROTECTION", "recapLockedCropForRatio",
    "normalizeEdgeVoiceKey", "ddConfigExtras", "canUseRecapCloneEngine",
    "canUseRecapRenderQuality", body
  );
  return fn(
    "https://example.com/v.mp4", state, { getElementById: (id) => dom[id] || null },
    RECAP_LOCKED_FILL, RECAP_LOCKED_MIRROR, RECAP_LOCKED_ZOOM,
    RECAP_LOCKED_PROTECTION, recapLockedCropForRatio, normalizeEdgeVoiceKey,
    ddConfigExtras, canUseRecapCloneEngine, canUseRecapRenderQuality
  );
};
console.log(JSON.stringify({
  vox_persona: run({ recapTtsEngine: "voxcpm", recapVoice: "mahnin", recapRatio: "9:16" }),
  vox_own: run({ recapTtsEngine: "voxcpm", recapOwnVoiceSelected: true, recapOwnVoiceStatus: "ready", recapOwnVoiceRefId: "webapp_ref_u1_abc" }),
  gemini_auto: run({ recapTtsEngine: "gemini", recapGeminiVoice: "", recapRatio: "16:9", recapScriptReview: true }),
  gemini_charon: run({ recapTtsEngine: "gemini", recapGeminiVoice: "Charon", recapRatio: "original" }),
  edge_ava: run({ recapTtsEngine: "edge", recapEdgeVoice: "ava", recapStoryStyle: "viral", recapSpeed: "+20%" }),
}));
""",
            stdin_text=js_src,
        )

    assert _run_scenarios(APP_JS) == _run_scenarios(base_js)
