# FULL UI REBUILD V2 — Phase 4 Report (Voice Clone & TTS Studios)

- **Branch:** `local/phase4-voice-tts` (stacked on `local/phase3-recap` `76ba302`)
- **Base Anchor:** `main` (`8d3248c`, PR #187 / `r143`) and original pre-V2 baseline `b4ec0f5` (`r142`)
- **Static Cache Token:** `20261113_r145` -> `20261113_r146`
- **Scope:** Liquid Glass Ceramic design pass for `#tab-voice` (Studio Cobalt accent, Curated Persona grid & filters, Custom Clone 5-Step guided flow & requirement card, Speed capsule, Excess banner, Sticky Cobalt CTA across Light Ceramic, Dark Glass, and Lite Ceramic) and `#tab-tts` (Studio Teal accent, Engine picker, Gemini/Edge controls, BYOK Accordion pill & API Key card, Speed capsule, Excess banner, and Teal CTA).

---

## 1. Visual Fixes & Polish Highlights

1. **Custom Clone Stepper & Requirement Card Contrast Fix:**
   - Replaced dark slate/navy fallback blocks inside `#tab-voice .vcp-clone-step` and `#tab-voice .vcp-clone-req-box` with crisp Light Ceramic translucent surfaces (`#F8FAFF` / `#FFFFFF` with Cobalt hairline borders and slate `#0F172A` / `#334155` text), preserving rich Dark Glass slate styling under `html[data-theme="dark"]`.
2. **Persona Grid & Batch BD Selector Preservation:**
   - Styled curated persona cards via `#tab-voice #personas-grid > [data-voice-id]` (Cobalt active glow ring, `#1D4ED8` category pill badge, `:empty` badge suppression so unbadged cards show zero blank outline pills, and single-row `.voice-preview-btn` >= `44px` tap target) so legacy `.persona-card` contract in `tests/unit/test_batch_bd_2026_11_06.py` stays 100% untouched.
3. **Dark Glass Floating CTA & Speed Capsules:**
   - Fixed `.sticky-cta-bar` light wash under `html[data-theme="dark"]` (`rgba(11, 18, 34, 0.94)` with frosted rim) and aligned Voice (`Cobalt`) vs TTS (`Teal`) `.speed-capsule` active thumbs and `.vcp-excess-banner` callouts across Light, Dark, and Lite modes.

---

## 2. Files Modified & Added

### Production Static Assets (Zero Python / Backend / API Changes)
- `app/webapp/static/css/style.css`:
  - Appended `/* VCP UI REBUILD V2 — PHASE 4: VOICE & TTS STUDIOS (LIGHT / DARK / LITE) */` (zero `backdrop-filter` / `-webkit-backdrop-filter` inside scrolling card rules).
- `app/webapp/static/index.html`:
  - Bumped all 4 cache tokens from `?v=20261113_r145` to `?v=20261113_r146`.

### Tests Added & Updated
- Added `tests/unit/test_phase_4_v2_voice_tts.py` (5 structural & guardrail tests checking `r146` cache token, zero `backdrop-filter` in `PHASE4_CSS`, Cobalt `#tab-voice` & Teal `#tab-tts` rules, Dark/Lite selectors, and preserved DOM IDs/handlers).
- Updated cache-token pins (`145` -> `146`) across 86 test files, split-literal canary pin in `tests/unit/test_batch_cb_2026_11_07.py` (`146`), and stale `20261113_r145` assertions in `test_phase_1_v2_liquid_glass.py`, `test_phase_2_v2_home.py`, and `test_phase_3_v2_recap.py`.

---

## 3. Hard Guardrail Audits (Measured After Final Edit)

### A. Element `id` & `onclick` Parity
- **vs `main` (`8d3248c`):** `ids: 724 -> 726` (`missing_ids=[]`, `new_ids=['home-hero-card', 'home-start-recap-tile']`); `onclick: 191 -> 191` (`missing_onclick=[]`).
- **vs `b4ec0f5a` (`r142`):** `ids: 723 -> 726` (`missing_ids=[]`, `new_ids=['btn-theme-toggle', 'home-hero-card', 'home-start-recap-tile']`); `onclick: 190 -> 191` (`missing_onclick=[]`).

### B. Canonical LF Asset Size vs `b4ec0f5a` (`r142`) — Cap <= +15.0%

| File | `b4ec0f5a` Bytes | Phase 4 Bytes | Delta | Growth |
|---|---:|---:|---:|---:|
| `app/webapp/static/css/style.css` | 341,120 | 421,308 | +80,188 | +23.51% |
| `app/webapp/static/js/app.js` | 777,042 | 786,167 | +9,125 | +1.17% |
| `app/webapp/static/js/i18n.js` | 233,938 | 235,427 | +1,489 | +0.64% |
| **Total CSS + JS** | **1,352,100** | **1,442,902** | **+90,802** | **+6.72%** |
| `app/webapp/static/index.html` | 310,784 | 316,195 | +5,411 | +1.74% |

- Cap allowed (`+15.0%`): `1,554,914` bytes. Measured CSS + JS total is **`+6.72%`** (`112,012` bytes headroom remaining for Phase 5).
- Non-chrome `backdrop-filter` in `V2_CSS`: `[]` (empty).
- `style.css` braces balanced: `True (2370 / 2370)`.
- `node --check` on `app.js` and `i18n.js`: exit `0`.
- `python -m compileall -q app`: exit `0`.

---

## 4. Playwright Mobile Visual Verification (`phase4_after`, `10` Captures at `360px` & `380px`)

Verified `scrollWidth === innerWidth` (`iw == sw`, `personas=4`, `errs=0`) across all 10 scenarios, and visually inspected PNGs:
- `02_voice_personas_open_360` & `05_voice_dark_360`: Crisp Studio Cobalt active ring, `:empty` badge hidden cleanly (`နှင်းဝေ`), balanced 2-col Persona cards in Light Ceramic and Midnight Navy Dark Glass.
- `04_voice_custom_clone_360`: High-contrast Light Ceramic 5-step guided flow pills and dashed upload zone.
- `06_voice_lite_380`: Crisp opaque Lite Ceramic Voice layout.
- `07_tts_gemini_vip_360`, `08_tts_byok_open_360`, `09_tts_dark_360`, `10_tts_lite_380`: Studio Teal badge, BYOK Accordion pill, readable dark input/button borders, and Teal speed capsule across Light Glass, Dark Glass, and Lite Ceramic.

---

## 5. Verification Results (Post-Final-Edit)

- **Targeted UI / V2 / Phase / Batch Suites (19 files):** `193 passed in 31.65s`
- **Recap Viewport Fit Non-libraqm Subset (`test_batch_dd_2026_11_09.py -k "not test_fit_ and not test_preview_fit_"`):** `31 passed, 3 deselected in 2.74s`
- **Phase 1–3 Regression Playwright Sweep (`phase4_reg_after`, `22` Captures at `360px` & `380px`):** `iw == sw` across all 22 scenarios, MUST-FIX #1/#2/#3 checks verified clean (`overlap: false`, `subInside: true`, all 5 deck tabs clickable `hitSelf: true`, `ratio-16-9` `16 / 9`).
- **Linux Gate:** Remaining Windows broad suite exclusions (`tzdata` / `libraqm`) and full-suite verification remain gated on Hammock's Linux runner.
