"""Unit tests for Post-Combined Follow-Up Part A: Mini App cold-open splash & greeting."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT / "app" / "webapp" / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC_DIR / "js" / "i18n.js").read_text(encoding="utf-8")


def _extract_splash_controller_js() -> str:
    start = APP_JS.index("// ── Part A: Cold-Open Splash Controller")
    end = APP_JS.index("// ── Telegram WebApp Initialization", start)
    return APP_JS[start:end]


def _run_node_scenario(script_body: str) -> dict:
    ctrl_src = _extract_splash_controller_js()
    harness = f"""
const vm = require("vm");

function runScenario() {{
  let nowMs = 1000;
  let nextTimerId = 1;
  const timers = new Map();
  function fakeSetTimeout(fn, delay) {{
    const id = nextTimerId++;
    timers.set(id, {{ fn, at: nowMs + Math.max(0, Number(delay) || 0), id }});
    return id;
  }}
  function fakeClearTimeout(id) {{
    timers.delete(id);
  }}
  function advanceBy(ms) {{
    const target = nowMs + ms;
    while (true) {{
      let earliest = null;
      for (const item of timers.values()) {{
        if (item.at <= target && (!earliest || item.at < earliest.at || (item.at === earliest.at && item.id < earliest.id))) {{
          earliest = item;
        }}
      }}
      if (!earliest) break;
      timers.delete(earliest.id);
      nowMs = earliest.at;
      earliest.fn();
    }}
    nowMs = target;
  }}

  class ClassList {{
    constructor() {{ this.set = new Set(); }}
    add(...cls) {{ cls.forEach(c => this.set.add(c)); }}
    remove(...cls) {{ cls.forEach(c => this.set.delete(c)); }}
    toggle(c, force) {{
      if (force === undefined) {{ if (this.set.has(c)) this.set.delete(c); else this.set.add(c); return; }}
      if (force) this.set.add(c); else this.set.delete(c);
    }}
    contains(c) {{ return this.set.has(c); }}
  }}
  class El {{
    constructor(id) {{
      this.id = id;
      this.classList = new ClassList();
      this.attrs = {{}};
      this.listeners = {{}};
      this.removed = false;
      this.innerText = "";
    }}
    setAttribute(k, v) {{ this.attrs[k] = String(v); }}
    getAttribute(k) {{ return this.attrs[k] ?? null; }}
    hasAttribute(k) {{ return k in this.attrs; }}
    removeAttribute(k) {{ delete this.attrs[k]; }}
    addEventListener(evt, fn) {{ (this.listeners[evt] = this.listeners[evt] || []).push(fn); }}
    dispatch(evt, evObj = {{}}) {{ (this.listeners[evt] || []).forEach(fn => fn(evObj)); }}
    remove() {{ this.removed = true; delete elements[this.id]; }}
  }}

  const splashEl = new El("vcp-splash");
  const gateLockEl = new El("gate-lock");
  gateLockEl.classList.add("hidden");
  const gateBannerEl = new El("gate-banner");
  gateBannerEl.classList.add("hidden");
  const bodyEl = new El("body");
  bodyEl.setAttribute("data-splash-active", "1");
  bodyEl.classList.add("gate-locked");
  const docEl = new El("html");
  const elements = {{
    "vcp-splash": splashEl,
    "gate-lock": gateLockEl,
    "gate-banner": gateBannerEl,
  }};

  const audioInstances = [];
  let audioPlayBehavior = "resolve";
  class FakeAudio {{
    constructor(src) {{
      this.src = src;
      this.preload = "";
      this.loaded = false;
      this.playCalled = false;
      this.onended = null;
      this.onerror = null;
      audioInstances.push(this);
    }}
    load() {{ this.loaded = true; }}
    play() {{
      this.playCalled = true;
      if (audioPlayBehavior === "throw") throw new Error("Audio blocked");
      if (audioPlayBehavior === "reject") return Promise.reject(new Error("NotAllowedError"));
      return Promise.resolve();
    }}
  }}

  const storage = {{}};
  const ctx = vm.createContext({{
    Date: {{ now: () => nowMs }},
    setTimeout: fakeSetTimeout,
    clearTimeout: fakeClearTimeout,
    Math,
    Promise,
    Error,
    state: {{}},
    entryGate: {{ locked: true, mode: "member" }},
    currentLang: undefined,
    localStorage: {{
      getItem: (k) => (k in storage ? storage[k] : null),
      setItem: (k, v) => {{ storage[k] = String(v); }},
    }},
    document: {{
      documentElement: docEl,
      body: bodyEl,
      getElementById: (id) => elements[id] || null,
    }},
    Audio: FakeAudio,
    window: null,
    updateGateBanner: null,
    showGateReminder: null,
    showOpenViaTelegramModal: null,
  }});
  ctx.window = ctx;
  vm.runInContext({json.dumps(ctrl_src)}, ctx);

  // Pull real lockEntryGate + updateGateBanner logic structure into context
  ctx.lockEntryGate = function() {{
    const overlay = ctx.document.getElementById("gate-lock");
    const blockedBySplash = typeof ctx.isSplashBlockingOverlays === "function" && ctx.isSplashBlockingOverlays();
    if (overlay) {{
      if (blockedBySplash) overlay.classList.add("hidden");
      else overlay.classList.remove("hidden");
    }}
    ctx.document.body.classList.add("gate-locked");
    ctx.entryGate.locked = true;
  }};
  ctx.updateGateBanner = function(data) {{
    const banner = ctx.document.getElementById("gate-banner");
    if (!banner || !data) return;
    if (typeof ctx.isSplashBlockingOverlays === "function" && ctx.isSplashBlockingOverlays()) {{
      ctx.state._pendingGateBannerData = data;
      banner.classList.add("hidden");
      return;
    }}
    banner.classList.remove("hidden");
  }};

  {script_body}
}}
console.log(JSON.stringify(runScenario()));
"""
    proc = subprocess.run(
        ["node", "-e", harness],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(ROOT),
    )
    return json.loads(proc.stdout.strip())


def test_part_a_splash_markup_critical_css_and_assets():
    # Inline critical CSS lives in <head> BEFORE style.css
    head_html = INDEX_HTML.split("</head>", 1)[0]
    assert '<style id="vcp-splash-critical-css">' in head_html
    assert head_html.index('<style id="vcp-splash-critical-css">') < head_html.index("/static/css/style.css")

    # Critical CSS gates overlays, honors reduced-motion, and matches theme bg-deep
    style_m = re.search(r'<style id="vcp-splash-critical-css">([\s\S]*?)</style>', head_html)
    assert style_m is not None
    crit_css = style_m.group(1)
    assert "position: fixed" in crit_css
    assert "inset: 0" in crit_css
    assert "prefers-reduced-motion" in crit_css
    assert "#dcebf7" in crit_css and "#070B16" in crit_css
    assert 'body[data-splash-active="1"] #gate-lock' in crit_css
    assert 'body[data-splash-active="1"] #gate-banner' in crit_css

    # #vcp-splash is the FIRST element inside <body> before #app
    body_after = INDEX_HTML.split("<body", 1)[1].split(">", 1)[1].lstrip()
    assert body_after.startswith('<div id="vcp-splash"')
    assert INDEX_HTML.index('id="vcp-splash"') < INDEX_HTML.index('id="app"')

    # Approved mockup v6 anatomy: reused brand-logo.svg, wordmark, tagline, 7 eq bars, progress line, MY + EN tap hints, no emojis
    splash_block = INDEX_HTML[INDEX_HTML.index('id="vcp-splash"') : INDEX_HTML.index('<div id="app"')]
    assert "/static/img/brand-logo.svg" in splash_block
    assert "VOICE CLONE" in splash_block and "PRO" in splash_block
    assert "AI VOICE &amp; RECAP STUDIO" in splash_block
    assert splash_block.count('class="vcp-splash-eq-bar"') == 7
    assert 'class="vcp-splash-progress-bar"' in splash_block
    assert "စတင်ရန် တစ်ချက် နှိပ်ပါ" in splash_block
    assert "Tap to start" in splash_block
    assert not re.search(r"[\U0001F300-\U0001FAFF]", splash_block)

    # Spoken welcome greeting assets exist at referenced paths and are non-trivial MP3s
    for loc, min_size in (("my", 40000), ("en", 35000)):
        mp3 = STATIC_DIR / "audio" / f"welcome_{loc}.mp3"
        assert mp3.is_file(), f"Missing {mp3}"
        assert mp3.stat().st_size >= min_size


def test_part_a_splash_never_hides_before_ready_or_without_tap():
    # 1. Stub a slow boot + no tap for 30s after READY -> splash remains visible
    res_untapped = _run_node_scenario(
        """
        ctx.lockEntryGate();
        ctx.updateGateBanner({ is_admin: false, membership_verified: false });
        const beforeReadyHidden = ctx.__vcpSplash.isHidden();
        const gateBefore = gateLockEl.classList.contains("hidden");
        const bannerBefore = gateBannerEl.classList.contains("hidden");
        ctx.__vcpSplash.markBootReady();
        const rightAfterReadyHidden = ctx.__vcpSplash.isHidden();
        advanceBy(30000);
        return {
          beforeReadyHidden,
          gateBefore,
          bannerBefore,
          rightAfterReadyHidden,
          stillVisible30sAfterReady: !ctx.__vcpSplash.isHidden() && !splashEl.removed,
          gateStillHidden: gateLockEl.classList.contains("hidden"),
          bannerStillHidden: gateBannerEl.classList.contains("hidden"),
        };
        """
    )
    assert res_untapped == {
        "beforeReadyHidden": False,
        "gateBefore": True,
        "bannerBefore": True,
        "rightAfterReadyHidden": False,
        "stillVisible30sAfterReady": True,
        "gateStillHidden": True,
        "bannerStillHidden": True,
    }


def test_part_a_splash_tap_locale_audio_waits_for_clip_and_ready():
    # Case A: unset locale defaults to MY, tap before READY -> clip ends -> waits for READY -> fades 300ms
    res_slow = _run_node_scenario(
        """
        ctx.lockEntryGate();
        ctx.updateGateBanner({ is_admin: false, membership_verified: false });
        const preloadedSrc = audioInstances.map(a => a.src);
        splashEl.dispatch("click");
        const played = audioInstances.find(a => a.playCalled);
        // Clip ends at 2s, BEFORE bootReady
        advanceBy(2000);
        played.onended();
        const afterClipBeforeReadyRemoved = splashEl.removed;
        // Now boot READY lands at 2.5s
        advanceBy(500);
        ctx.__vcpSplash.markBootReady();
        const hidingStarted = splashEl.classList.contains("vcp-splash-hiding");
        const removedBeforeFade = splashEl.removed;
        advanceBy(320);
        return {
          preloadedSrc,
          playedSrc: played ? played.src : null,
          afterClipBeforeReadyRemoved,
          hidingStarted,
          removedBeforeFade,
          removedAfterFade: splashEl.removed,
          splashActiveAttrAfter: bodyEl.hasAttribute("data-splash-active"),
          gateUnhiddenAfter: !gateLockEl.classList.contains("hidden"),
          bannerUnhiddenAfter: !gateBannerEl.classList.contains("hidden"),
        };
        """
    )
    assert res_slow["preloadedSrc"] == ["/static/audio/welcome_my.mp3"]
    assert res_slow["playedSrc"] == "/static/audio/welcome_my.mp3"
    assert res_slow["afterClipBeforeReadyRemoved"] is False
    assert res_slow["hidingStarted"] is True
    assert res_slow["removedBeforeFade"] is False
    assert res_slow["removedAfterFade"] is True
    assert res_slow["splashActiveAttrAfter"] is False
    assert res_slow["gateUnhiddenAfter"] is True
    assert res_slow["bannerUnhiddenAfter"] is True

    # Case B: EN locale plays welcome_en.mp3, tap AFTER READY waits until clip ends
    res_en = _run_node_scenario(
        """
        ctx.currentLang = "en";
        ctx.__vcpSplash.onLocaleChanged("en");
        ctx.__vcpSplash.markBootReady();
        advanceBy(500);
        splashEl.dispatch("click");
        const played = audioInstances.filter(a => a.playCalled).pop();
        advanceBy(3500);
        const stillUpWhilePlaying = !splashEl.removed;
        played.onended();
        advanceBy(320);
        return {
          playedSrc: played ? played.src : null,
          stillUpWhilePlaying,
          removedAfterClipEnd: splashEl.removed,
        };
        """
    )
    assert res_en == {
        "playedSrc": "/static/audio/welcome_en.mp3",
        "stillUpWhilePlaying": True,
        "removedAfterClipEnd": True,
    }


def test_part_a_splash_clip_failure_and_post_tap_safety_valve():
    # 1. Clip play rejects -> fades ~350ms after tap once READY
    res_fail = _run_node_scenario(
        """
        audioPlayBehavior = "reject";
        ctx.__vcpSplash.markBootReady();
        advanceBy(500);
        splashEl.dispatch("click");
        await_dummy: {}
        // Let microtask rejection schedule failTimer
        return Promise.resolve().then(() => {
          advanceBy(200);
          const upAt200 = !splashEl.removed;
          advanceBy(200); // total 400ms >= 350ms -> hiding started
          const hidingAt400 = splashEl.classList.contains("vcp-splash-hiding");
          advanceBy(320);
          return { upAt200, hidingAt400, removedAfterFade: splashEl.removed };
        });
        """
    ) if False else None
    assert res_fail is None  # handled via async node runner below

    # Use async node check for Promise.reject + post-tap 10s safety valve
    ctrl_src = _extract_splash_controller_js()
    script = f"""
const vm = require("vm");
(async () => {{
  function makeEnv(behavior) {{
    let nowMs = 1000, nextId = 1;
    const timers = new Map();
    const fakeSetTimeout = (fn, d) => {{ const id = nextId++; timers.set(id, {{ fn, at: nowMs + Math.max(0, Number(d) || 0), id }}); return id; }};
    const fakeClearTimeout = (id) => timers.delete(id);
    const advanceBy = (ms) => {{
      const target = nowMs + ms;
      while (true) {{
        let earliest = null;
        for (const item of timers.values()) {{
          if (item.at <= target && (!earliest || item.at < earliest.at || (item.at === earliest.at && item.id < earliest.id))) earliest = item;
        }}
        if (!earliest) break;
        timers.delete(earliest.id);
        nowMs = earliest.at;
        earliest.fn();
      }}
      nowMs = target;
    }};
    class CL {{ constructor() {{ this.s = new Set(); }} add(...c){{c.forEach(x=>this.s.add(x));}} remove(...c){{c.forEach(x=>this.s.delete(x));}} contains(c){{return this.s.has(c);}} }}
    const splashEl = {{ id: "vcp-splash", classList: new CL(), attrs: {{}}, listeners: {{}}, removed: false,
      addEventListener(e, fn) {{ (this.listeners[e]=this.listeners[e]||[]).push(fn); }},
      dispatch(e) {{ (this.listeners[e]||[]).forEach(fn => fn({{}})); }},
      remove() {{ this.removed = true; }}
    }};
    const bodyEl = {{ attrs: {{"data-splash-active":"1"}}, hasAttribute:k=>k in bodyEl.attrs, removeAttribute:k=>delete bodyEl.attrs[k], classList: new CL() }};
    const docEl = {{ attrs: {{}}, setAttribute:(k,v)=>{{docEl.attrs[k]=v;}} }};
    const audios = [];
    class FakeAudio {{
      constructor(src) {{ this.src = src; this.onended = null; this.onerror = null; audios.push(this); }}
      load() {{}}
      play() {{ return behavior === "reject" ? Promise.reject(new Error("blocked")) : Promise.resolve(); }}
    }}
    const ctx = vm.createContext({{
      Date: {{ now: () => nowMs }}, setTimeout: fakeSetTimeout, clearTimeout: fakeClearTimeout, Math, Promise, Error,
      state: {{}}, entryGate: {{ locked: false }}, currentLang: "my",
      localStorage: {{ getItem: () => null, setItem: () => {{}} }},
      document: {{ documentElement: docEl, body: bodyEl, getElementById: id => id === "vcp-splash" ? (splashEl.removed ? null : splashEl) : null }},
      Audio: FakeAudio, window: null,
    }});
    ctx.window = ctx;
    vm.runInContext({json.dumps(ctrl_src)}, ctx);
    return {{ ctx, splashEl, audios, advanceBy }};
  }}

  // 1) Play rejection -> fades ~350ms after tap
  const e1 = makeEnv("reject");
  e1.ctx.__vcpSplash.markBootReady();
  e1.advanceBy(500);
  e1.splashEl.dispatch("click");
  await new Promise(r => setImmediate(r));
  e1.advanceBy(200);
  const upAt200 = !e1.splashEl.classList.contains("vcp-splash-hiding");
  e1.advanceBy(160);
  const hidingAt360 = e1.splashEl.classList.contains("vcp-splash-hiding");
  e1.advanceBy(320);

  // 2) Post-tap safety valve: READY never lands -> fades 10s after clip ends
  const e2 = makeEnv("resolve");
  e2.advanceBy(500);
  e2.splashEl.dispatch("click");
  e2.advanceBy(4000);
  e2.audios[0].onended();
  e2.advanceBy(9500);
  const stillUpAt9_5s = !e2.splashEl.removed && !e2.splashEl.classList.contains("vcp-splash-hiding");
  e2.advanceBy(600);
  const hidingAt10_1s = e2.splashEl.classList.contains("vcp-splash-hiding");
  e2.advanceBy(320);

  console.log(JSON.stringify({{
    upAt200,
    hidingAt360,
    removedAfterRejectFade: e1.splashEl.removed,
    stillUpAt9_5s,
    hidingAt10_1s,
    removedAfterSafety: e2.splashEl.removed,
  }}));
}})();
"""
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(ROOT),
    )
    out = json.loads(proc.stdout.strip())
    assert out == {
        "upAt200": True,
        "hidingAt360": True,
        "removedAfterRejectFade": True,
        "stillUpAt9_5s": True,
        "hidingAt10_1s": True,
        "removedAfterSafety": True,
    }


def test_part_a_boot_hooks_and_overlay_gating_wired():
    # loadUserProfile hooks notifyProfileApplied in finally after data-profile-loading cleanup
    assert "notifyProfileApplied" in APP_JS
    assert "notifyInitialTabRendered" in APP_JS
    assert "isSplashBlockingOverlays" in APP_JS
    assert "flushDeferredGateAndBanner" in APP_JS
    # i18n setLanguage keeps data-lang and splash locale in sync
    assert 'setAttribute("data-lang"' in I18N_JS
    assert "onLocaleChanged" in I18N_JS
