# FULL UI REBUILD V2 — Phase 5 Report (Downloader, VIP, History, Modals & Admin Console)

- **Branch:** `local/phase5-rest-admin` (stacked on `local/phase4-voice-tts` `f2ddf1c`)
- **Base Anchor:** `main` (`8d3248c`, PR #187 / `r143`) and original pre-V2 baseline `b4ec0f5a` (`r142`)
- **Static Cache Token:** `20261113_r146` -> `20261113_r147`
- **Scope:** Liquid Glass Ceramic completion across `#tab-downloader` (Indigo Studio), `#tab-vip` (Champagne Gold plans + payment drawer), `#tab-history` + `.live-jobs-pill`, Modals & Sheets (`.submit-confirm-card`, `.live-jobs-sheet-card`, `#income-detail-sheet`, `.image-modal`, `.gate-lock-card`), and `#tab-admin` (symmetrical **4x2 Ceramic navigation grid** `#adm-nav`, Overview **Action Triage banner** `#adm-triage-card`, KPI mini cards `.stat-mini-card`, `.adm-seg` filter pills, and **7 collapsible native `<details class="adm-accordion">` Settings drawers** with persistent sticky save button) across Light Ceramic, Dark Glass (`html[data-theme="dark"]`), and Lite Ceramic (`html.lite` / `html[data-theme="dark"].lite`).

---

## 1. Visual Fixes & Architecture Highlights

1. **Admin Navigation Grid Symmetry (`#tab-admin #adm-nav`):**
   - Replaced the uneven `4+3+1` flex-wrap inside an outer border box with a symmetrical **4-column x 2-row Ceramic grid** (`grid-template-columns: repeat(4, minmax(0, 1fr))`), eliminating orphan 3rd-row tiles at `360px` and `380px`.
2. **Overview Action Triage Banner (`#adm-triage-card`):**
   - Added a compact top triage banner on `#adm-screen-overview` wired inside `loadAdminData()` (`#adm-triage-title`, `#adm-triage-sub`, `.is-clear` toggle, and `#btn-adm-triage-review` -> `showAdminSection('payments')`) so pending slip approvals surface immediately on open without altering backend endpoints.
3. **Collapsible Admin Settings Accordions (`#adm-screen-settings`):**
   - Grouped all admin settings sections into 7 native `<details class="card glass vcp-card adm-accordion">` accordions (`#adm-acc-pay-maint` open by default, `#adm-acc-ai-bots`, `#adm-acc-quotas-gates`, `#adm-acc-tutorials`, `#adm-acc-pricing`, `#adm-acc-gemini`, `#adm-tts-chain-card`), while keeping `#btn-save-admin-settings` (`onclick="saveAdminSettings()"`) visible outside collapsed drawers and preserving every form control ID/handler.
4. **Remaining User Surfaces & Overlays:**
   - Styled `#tab-downloader` (`.dl-search-card`, `.dl-formats-grid .vcp-dl-format-card.active`), `#tab-vip` (`.stats-card`, `.pricing-card`, `#payment-box`), `#tab-history` (`.vcp-history-retention-bar`), and `.submit-confirm-card` / `.live-jobs-sheet-card` / `.gate-lock-card` consistently across Light Ceramic, Dark Glass, and Lite Ceramic.

---

## 2. Files Modified & Added

### Production Static Assets (Zero Python / Backend / API Changes)
- `app/webapp/static/css/style.css`:
  - Appended `/* VCP UI REBUILD V2 — PHASE 5: DOWNLOADER, VIP, HISTORY, MODALS & ADMIN CONSOLE */` (+503 lines, zero `backdrop-filter` / `-webkit-backdrop-filter` inside scrolling card rules).
- `app/webapp/static/index.html`:
  - Bumped all 4 cache tokens from `?v=20261113_r146` to `?v=20261113_r147`.
  - Added `#adm-triage-card` and wrapped `#adm-screen-settings` groups into 7 `<details class="card glass vcp-card adm-accordion">` accordions without removing any existing element `id` or `onclick`.
- `app/webapp/static/js/app.js`:
  - Wired `#adm-triage-card`, `#adm-triage-title`, `#adm-triage-sub`, and `.is-clear` state toggle in `loadAdminData()`.
- `app/webapp/static/js/i18n.js`:
  - Added 11 bilingual (`my` + `en`) keys: `adm_triage_checking`, `adm_triage_action_needed`, `adm_triage_clear`, `adm_triage_sub`, `adm_triage_btn`, `adm_set_grp_pay_maint`, `adm_set_pill_pay_maint`, `adm_set_grp_ai_bots`, `adm_set_pill_ai_bots`, `adm_set_grp_quotas_gates`, `adm_set_pill_quotas`.

### Tests Added & Updated
- Added `tests/unit/test_phase_5_v2_rest_admin.py` (5 structural, guardrail, Dark/Lite selector, accordion, and bilingual i18n checks).
- Updated cache-token pins (`146` -> `147`) across 87 test files (`Remaining hits for 146: 0` across test pins, plus split-literal canary `"20261113_r" + "147"` in `tests/unit/test_batch_cb_2026_11_07.py`), and updated retired `r146` assertions in `test_phase_1_v2_liquid_glass.py`, `test_phase_2_v2_home.py`, `test_phase_3_v2_recap.py`, and `test_phase_4_v2_voice_tts.py`.

---

## 3. Hard Guardrail Audits (Measured After Final Edit)

### A. Element `id` & `onclick` Parity
- **vs `main` (`8d3248c`):** `ids=724->737 missing_ids=[] onclick=191->191 missing_onclick=[]`
- **vs `b4ec0f5a` (`r142`):** `ids=723->737 missing_ids=[] onclick=190->191 missing_onclick=[]`

### B. Canonical LF Asset Size vs `b4ec0f5a` (`r142`) — Cap <= +15.0%

| File | `b4ec0f5a` Bytes | Phase 5 Bytes | Delta | Growth |
|---|---:|---:|---:|---:|
| `app/webapp/static/css/style.css` | 341,120 | 437,478 | +96,358 | +28.25% |
| `app/webapp/static/js/app.js` | 777,042 | 786,912 | +9,870 | +1.27% |
| `app/webapp/static/js/i18n.js` | 233,938 | 237,163 | +3,225 | +1.38% |
| **Total CSS + JS** | **1,352,100** | **1,461,553** | **+109,453** | **+8.10%** |
| `app/webapp/static/index.html` | 310,784 | 320,358 | +9,574 | +3.08% |

- Cap allowed (`+15.0%`): `1,554,914` bytes. Final Phase 5 CSS + JS total is **`1,461,553` bytes (`+8.10%`)**, leaving `93,361` bytes below cap.
- Non-chrome `backdrop-filter` in V2 CSS: `[]` (empty).
- `style.css` braces balanced: `True (2448/2448)`.
- `node --check` on `app.js` and `i18n.js`: exit `0`.
- `python -m compileall`: exit `0`.

---

## 4. Playwright Mobile Visual Verification (`360px` & `380px`)

- **Phase 5 Surfaces (`phase5_after`, 13 captures):** All 13 scenarios reported `iw == sw`, `groups=7`, `errs=0` (Downloader, VIP plans + Dark payment drawer, History, Live Jobs sheet, Admin Overview/Payments/Jobs/Users/Settings Light & Dark Glass, Lite Ceramic Overview `380px`, and Submit Confirm modal), and all 13 PNGs were visually inspected.
- **Phase 1–3 Regression Sweep (`phase5_reg_after`, 22 captures):** All 22 scenarios reported `iw == sw` with MUST-FIX #1 (`overlap: false`, `subInside: true`), MUST-FIX #2 (`hitSelf: true` across all 5 Step-2 deck category tabs), and MUST-FIX #3 (`ratio-16-9`, `16 / 9`, `ratioActual: 1.778`, `tabsClickableInSticky: true`) passing.
- **Phase 4 Voice & TTS Regression Sweep (`phase5_voice_reg_after`, 10 captures):** All 10 scenarios reported `iw == sw`, `personas=4`, `errs=0`.

---

## 5. Verification Results (Post-Final-Edit)

- **Targeted UI / i18n / Admin Settings / Phase 5 Suites (22 files):** `228 passed in 45.35s`
- **Recap Viewport Fit Non-libraqm Subset (`test_batch_dd_2026_11_09.py -k "not test_fit_ and not test_preview_fit_"`):** `31 passed, 3 deselected in 1.95s`
- **Broad 132-File Windows UI Pytest Sweep (`--continue-on-collection-errors`):** `1,886 passed, 1 skipped, 58 failed, 14 errors in 462.44s`. Inspected every failure and error: all 58 failures and 14 collection errors were Windows host environment exclusions (`tzdata` / `ZoneInfoNotFoundError` for `America/Los_Angeles` and cascading `app.handlers` `ImportError`, Pillow `libraqm` unavailable for Burmese shaping tests, and missing Windows `ffmpeg` binary in `test_batch_dp_2026_11_11.py`), with zero HTML/CSS/JS/i18n/UI failures.
- **Linux Gate:** Full Linux pytest suite and live production walk remain gated on Hammock's Linux runner before merge.
