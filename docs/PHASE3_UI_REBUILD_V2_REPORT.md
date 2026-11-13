# FULL UI REBUILD V2 — Phase 3 Report (Recap Studio & 3 MUST-FIX Repairs)

- **Branch:** `local/phase3-recap` (stacked on Phase 2 `90aee65`, pending PR #188 merge into `main`)
- **Base Anchor:** `main` (`8d3248c`, PR #187 / `r143`) and original pre-V2 baseline `b4ec0f5` (`r142`)
- **Static Cache Token:** `20261113_r144` -> `20261113_r145`
- **Scope:** Full Liquid Glass Ceramic styling pass for `#tab-recap` (Steps 1–4 progress stepper, drop-zone & mode toggles, Step-2 split-screen deck, presets/sub-style cards, engine/speaker controls, dual quote comparison cards, Dark Glass toast/card fixes, Lite Ceramic flat high-contrast mode) + all **3 Recap MUST-FIX items** verified in production by Hammock (2026-11-13).

---

## 1. Phase 3 MUST-FIX Repairs (Verified in Playwright & Unit Tests)

1. **MUST-FIX #1 — Preview bottom-strip collision (`9:16` and `16:9`) without changing output coordinates:**
   - **Root cause:** Default Blur Box 1 (`top: 85%; height: 7%`) overlaps the bottom subtitle zone (`bottom: 5%`), causing their center drag pills to collide and making the subtitle box unselectable / clipped by `#recap-mockup-frame`'s rounded bottom edge.
   - **Fix (`style.css` & `app.js`):** Separated the two editor grip handles horizontally inside `#tab-recap` (`#preview-blur-box .drag-grip-icon { left: 6px; }` sky pill vs `#preview-sub-box .sub-grip { right: 6px; }` amber pill), made `#preview-sub-box` `pointer-events: none` outside `.sub-grip` so underlying controls stay reachable, added `.is-near-top` flip logic in `syncBlurBoxVisual()` and `syncSubBoxVisual()` so top-dragged pills never clip above `top: 0`, and set `#recap-mockup-frame` inner radius to `5px` inside `#recap-mockup-wrap` dark bezel so bottom-5% subtitle bounds are never corner-clipped.
   - **Preview = Output preserved:** Default render coordinates (`preview-blur-box` `top: 85%; height: 7%`, subtitle `bottom: 5%`) and submit payload keys are untouched.
   - **Measured (`360px` & `380px`, `9:16` & `16:9`):** `overlap: false`, `subInside: true` (`9:16`: Blur grip `x=60.03..109.27, y=845.70..867.70`; Sub grip `x=250.72..299.95, y=874.09..896.09`; Frame `bottom=965.31` vs SubBox `bottom=939.00`).

2. **MUST-FIX #2 — Sticky preview covering Step-2 deck & sliding under floating header:**
   - **Root cause:** `.vcp-step2-sticky-preview` pinned at `top: 0; z-index: 15` (under `.top-nav` and over scrolling `.recap-dock-section` tabs).
   - **Fix (`style.css` & `app.js`):** Computed dynamic header bottom clearance (`--vcp-hdr-sticky-top`, default `114px`) inside `updateCanvasMockupVisuals()`, applied `top: var(--vcp-hdr-sticky-top, 114px); z-index: 6;` to `#tab-recap .vcp-step2-sticky-preview`, automatically un-pinned tall portrait `9:16` stages (`is-ratio-9-16`, `is-portrait-stage`) on mobile heights (`@media (max-width: 767px) and (max-height: 960px)`), and raised `#tab-recap .recap-dock-section` to `position: relative; z-index: 18;` with opaque light/dark card surfaces.
   - **Measured (`360px` scrolled Step 2):** All 5 deck tabs (`preset`, `frame`, `subtitle`, `blur`, `protection`) report `hitSelf: true` via `elementFromPoint`.

3. **MUST-FIX #3 — Master Preset `"YouTube Landscape 16:9"` applying without updating preview/summary:**
   - **Root causes (2):**
     1. Top-level `applyRecapPresetObject()` referenced `RECAP_SETTINGS_KEY`, which was block-scoped inside `DOMContentLoaded` and threw a swallowed `ReferenceError`, aborting persistence/sync while `applyRecapMasterPreset()` still toasted success.
     2. `restoreRecapSettings()` was not exposed globally and omitted visual canvas/frame/Step-2 summary/Step-4 sync calls after restoring control values.
   - **Fix (`app.js`):** Exposed `window.__RECAP_SETTINGS_KEY`, `window.restoreRecapSettings`, `window.__ddFitPreviewFrame`, `window.refreshRecapStep2Summary`, and `window.refreshRecapStep4Output`; wired `restoreRecapSettings()` to call `updateCanvasMockupVisuals()`, `ddFitPreviewFrame()`, `refreshRecapStep2Summary()`, `refreshRecapStep4Thumb()`, and `refreshRecapStep4Facts()`; and updated `applyRecapPresetObject(patch)` to persist cleanly, sync live state, refresh canvas/summary/Step-4/active category, and re-paint preset cards.
   - **Measured (`360px` tapping `"YouTube Landscape 16:9"`):** `cls="mockup-screen ratio-16-9"`, `aspectCss="16 / 9"`, `ratioActual=1.778`, `summary="16:9 · blur · 16pt · ⚪ Clean White (အဖြူ) · 🛡️ စံနှုန်း"`, `gripOverlap16_9=false`, `tabsClickableInSticky=true` (`tabsTop=131.66 > hdrBottom=110`).

---

## 2. Files Modified & Created

### Production Static Assets (Zero Python/Backend/API Changes)
- `app/webapp/static/index.html`:
  - Bumped all 4 cache tokens from `?v=20261113_r144` to `?v=20261113_r145`.
- `app/webapp/static/js/app.js`:
  - Exposed Recap settings key & helper functions globally, repaired `applyRecapPresetObject()` & `restoreRecapSettings()`, added `is-ratio-*` / `is-portrait-stage` / `--vcp-hdr-sticky-top` calculation in `updateCanvasMockupVisuals()`, and added `.is-near-top` edge handling in `syncBlurBoxVisual()` & `syncSubBoxVisual()`.
- `app/webapp/static/css/style.css`:
  - Added `/* VCP UI REBUILD V2 — PHASE 3: RECAP STUDIO & EDITOR (LIGHT / DARK / LITE) */` styling `#tab-recap` stepper pills, drop zone, mode tabs, Step-2 split preview + deck, preset/sub-style cards, dual quote comparison card, Dark Glass `.studio-toast`/`.floating-alert-card` & step-nav readability, single-line `.mini-btn-seek` pills, and Lite Ceramic overrides (zero `backdrop-filter` rules added inside `PHASE3_CSS`).

### Tests Updated / Added
- Added `tests/unit/test_phase_3_v2_recap.py` (5 structural & guardrail tests covering `r145` cache token, zero `backdrop-filter` in `PHASE3_CSS`, MUST-FIX #1/#2/#3 contracts, Dark & Lite `#tab-recap` selectors, and zero deleted `id`/`onclick`s vs `main`).
- Updated cache-token assertions (`144` -> `145`) across `tests/unit/` (plus retired `r144` checks in `test_phase_1_v2_liquid_glass.py` and `test_phase_2_v2_home.py` and canary pin `"20261113_r" + "145"` in `test_batch_cb_2026_11_07.py`).

---

## 3. Hard Guardrail Audits (Measured After Final Edit)

### A. Element `id` & `onclick` Parity
- **vs `main` (`8d3248c`):** `ids: 724 -> 726` (`missing_ids=[]`, `new_ids=['home-hero-card', 'home-start-recap-tile']`); `onclick: 191 -> 191` (`missing_onclick=[]`).
- **vs `b4ec0f5a` (`r142`):** `ids: 723 -> 726` (`missing_ids=[]`, `new_ids=['btn-theme-toggle', 'home-hero-card', 'home-start-recap-tile']`); `onclick: 190 -> 191` (`missing_onclick=[]`).

### B. Canonical LF Asset Size vs `b4ec0f5a` (`r142`) — Cap <= +15.0%

| File | `b4ec0f5a` Bytes | Phase 3 Bytes | Delta | Growth |
|---|---:|---:|---:|---:|
| `app/webapp/static/css/style.css` | 341,120 | 404,454 | +63,334 | +18.57% |
| `app/webapp/static/js/app.js` | 777,042 | 786,167 | +9,125 | +1.17% |
| `app/webapp/static/js/i18n.js` | 233,938 | 235,427 | +1,489 | +0.64% |
| **Total CSS + JS** | **1,352,100** | **1,426,048** | **+73,948** | **+5.47%** |
| `app/webapp/static/index.html` | 310,784 | 316,195 | +5,411 | +1.74% |

- Cap allowed (`+15.0%`): `1,554,914` bytes. Measured CSS + JS total is **`+5.47%`** (`128,866` bytes headroom remaining for Phases 4–5).
- Non-chrome `backdrop-filter` in `V2_CSS`: `[]` (empty).
- `style.css` braces balanced: `True (2283 { == 2283 })`.
- `node --check` on `app.js` and `i18n.js`: `0` syntax errors.

---

## 4. Playwright Mobile Visual Verification (`phase3_after`, `22` Captures at `360px` & `380px`)

Verified `scrollWidth === innerWidth` across all 22 scenarios (`0` horizontal overflow, `0` JS errors), and visually inspected updated PNG captures (`12_recap_step2_360.png`, `16_recap_step1_360.png`, `17_recap_step2_scrolled_360.png`, `18_recap_step2_yt16_9_360.png`, `19_recap_step2_dark_360.png`, `20_recap_step2_lite_380.png`, `21_recap_step3_360.png`, `22_recap_step4_360.png`):
- `16_recap_step1_360`: Clean Ceramic step rail (`22x22` circular step pills), drop zone & URL segmented control.
- `12_recap_step2_360`, `19_recap_step2_dark_360`, `20_recap_step2_lite_380`: Left/right separated Blur & Subtitle grips, unclipped subtitle bounds, high-contrast dark `.studio-toast` (`#0F172A` surface + `#F8FAFC` text).
- `17_recap_step2_scrolled_360`: All 5 Step-2 deck category tabs remain top-most clickable (`hitSelf: true`).
- `18_recap_step2_yt16_9_360`: `YouTube Landscape 16:9` applies cleanly (`16 / 9`, `ratioActual=1.778`), seek play button fits cleanly on a single line (`white-space: nowrap`), zero grip collision.
- `21_recap_step3_360` & `22_recap_step4_360`: Crisp Step-3 engine segmented control, balanced spacing, and Step-4 sticky violet CTA above floating dock.

---

## 5. Verification Results & Windows Disclosure

- **Targeted UI / V2 / Phase / Batch Suites (Post-Edit, 18 files):**
  - `test_phase_1_design_system_vcp.py`, `test_phase_1_v2_liquid_glass.py`, `test_phase_2_home_dashboard_vcp.py`, `test_phase_2_v2_home.py`, `test_phase_3_v2_recap.py`, `test_phase_3_voice_tts_vcp.py`, `test_phase_4_recap_editor_vcp.py`, `test_phase_5_recap_billing_and_ui_vcp.py`, `test_phase_6_dl_history_vip_vcp.py`, `test_phase_7_admin_vcp.py`, `test_home_polish_visual_vcp.py`, `test_batch_av_2026_11_05.py`, `test_batch_az_2026_11_06.py`, `test_batch_ba_2026_11_06.py`, `test_batch_bd_2026_11_06.py`, `test_batch_cf_2026_11_07.py`, `test_header_branding_batch_ad_2026_11_04.py`, `test_full_app_polish_2026_11_01.py` -> **`187 passed in 19.14s`**.
- **Recap Viewport Fit Non-libraqm Subset (`test_batch_dd_2026_11_09.py -k "not test_fit_ and not test_preview_fit_"`):**
  - **`31 passed, 3 deselected in 1.81s`**.
- **Broad Windows `ui_tests.txt` Sweep (130 UI-touching test files, `--continue-on-collection-errors`):**
  - **`1,875 passed, 58 failed, 1 skipped, 14 collection errors in 326.80s`**.
  - Every failure/collection error in that sweep on Windows stems from missing `tzdata` (`ZoneInfoNotFoundError: America/Los_Angeles`) or missing Pillow `libraqm`/subtitle PNG binary helpers on Windows (`test_batch_as`, `test_batch_ca`, `test_batch_cc`, `test_batch_da`, `test_batch_dd`, `test_free_adjust_batch_ag`, `test_r39_extract_geometry`, `test_subtitle_font_choice`, `test_subtitle_png_batch_q`, `test_admin_fixes_batch_r`, `test_batch_be`, `test_batch_bs`, `test_batch_cg`, `test_batch_dp`, `test_hunt6`, `test_predeploy_minifix`). **Zero HTML/CSS/JS/i18n/UI assertions failed.**
- **Linux Gate Required:** Hammock's Linux full-suite run remains the merge gate before pushing / merging Phase 4+.
