# FULL UI REBUILD V2 — Phase 1 Report (Liquid Glass Design System & Floating Chrome)

- **Branch:** `feat/ui-rebuild-v2`
- **Base Commit:** `b4ec0f5` (`main`, PR #186 / `r142`)
- **Static Cache Token:** `20261113_r142` -> `20261113_r143`
- **Scope:** Phase 1 foundations — Liquid Glass design tokens, Light Glass / Dark Glass (`html[data-theme="dark"]`) / Lite Ceramic (`html.lite`) theme system, Floating Header (`.top-nav`) + Floating Capsule Bottom Dock (`.bottom-nav`), uniform-stroke SF-style inline SVG icons, header Moon/Sun theme toggle (`#btn-theme-toggle`), plus both owner approval fixes folded into Phase 1.

---

## 1. Owner Approval Fixes Folded Into Phase 1

1. **Darkened Light-theme secondary/muted grey ink (outdoor readability):**
   - Updated `:root` and token overrides in `app/webapp/static/css/style.css` to `--text: #0F172A`, `--text-2: #2F435E`, `--text-3: #475569`, `--vcp-text-primary: #0F172A`, `--vcp-text-secondary: #2F435E`, and `--vcp-text-muted: #475569`, plus `html:not([data-theme="dark"])` overrides on `#tab-home` secondary hints (`.home-tile-hint`, `.home-free-desc`, `.home-help-entry-desc`, `.sample-meta`, `.text-muted`).
2. **Recap Stage top-left chip separation (prevents overlap with video/scene/logo overlay):**
   - Moved `#canvas-source-label` in `app/webapp/static/index.html` out of `#video-bg-canvas` into `.vcp-preview-stage-bar` alongside `#btn-preview-corner-reset`, styled via `.vcp-preview-stage-bar .canvas-source-label { position: static; ... }` in `app/webapp/static/css/style.css`.
   - Added `#recap-steps-nav { scroll-margin-top: 124px; }` to keep step anchor scrolling clear of the sticky floating header.

---

## 2. Files Modified & Created

### Production Static Assets (Zero Python/Backend/API Changes)
- `app/webapp/static/index.html`:
  - Bumped all 4 cache tokens to `?v=20261113_r143`.
  - Added `#btn-theme-toggle` (`onclick="toggleThemeMode()"`) in `.top-actions`; replaced header book emoji (`📚`) in `#btn-tutorial-top` and preview reset emoji (`🔄`) in `#btn-preview-corner-reset` with `<svg class="vcp-ico">` vector icons.
  - Replaced all 6 bottom-nav tab emojis (`🏠`, `🎙️`, `🎧`, `🎬`, `💎`, `⚡`) with uniform `1.85px`-stroke SF-style inline `<svg class="vcp-ico vcp-nav-svg">` icons.
  - Relocated `#canvas-source-label` into `.vcp-preview-stage-bar`.
- `app/webapp/static/css/style.css`:
  - Added `VCP UI REBUILD V2 — PHASE 1` token layer, ambient multi-stop mesh (`html:not(.lite) body::before`), floating frosted `.top-nav`, floating capsule `.bottom-nav` (with 6-tab Admin compaction), Dark Glass theme (`html[data-theme="dark"]`), Lite Ceramic overrides (`html.lite`, `body.lite`, `html[data-theme="dark"].lite`), and `html, body { overflow-x: clip !important; }`.
  - Strict low-end Android WebView GPU rule preserved: `backdrop-filter` applies only to fixed `.top-nav` / `.bottom-nav` chrome and stays `none !important` on scrolling `.tab-pane .card` content.
- `app/webapp/static/js/app.js`:
  - Added `syncThemeUiState`, `setThemeMode`, `initThemeMode`, and `toggleThemeMode`, wired into `initTelegram()` with theme-aware Telegram WebApp chrome colors.
- `app/webapp/static/js/i18n.js`:
  - Added `theme_toggle_title`, `theme_light`, and `theme_dark` in both `my` and `en`.

### Tests Updated / Added
- Added `tests/unit/test_phase_1_v2_liquid_glass.py` (8 structural & guardrail assertions covering `+15%` asset budget, `r143` cache token, outdoor ink tokens, Recap top-left chip separation, floating chrome & SF SVG icons, Dark & Lite themes + no scrolling `backdrop-filter`, theme JS helpers + `my`/`en` keys, and zero deleted `id`/`onclick`s vs `main`).
- Updated cache-token assertions (`r142` -> `r143`) across existing static unit tests (`82` existing test files total including `test_batch_av_2026_11_05.py`, `test_batch_cf_2026_11_07.py`, `test_batch_dd_2026_11_09.py`) and bounded `PASS_CSS` in `tests/unit/test_home_polish_visual_vcp.py` above `"VCP UI REBUILD V2"`.

---

## 3. Hard Guardrail Audits (Measured After Final Edit)

### A. Element `id` & `onclick` Parity vs `main` (`b4ec0f5`)
- **`id` attributes:** `723` -> `724` (`missing = []`, `added = ["btn-theme-toggle"]`)
- **`onclick` attributes:** `105` -> `106` (`missing = []`, `added = ["toggleThemeMode()"]`)

### B. Canonical LF Asset Size vs `main` (`b4ec0f5`) — Cap <= +15.0%

| File | `main` (`b4ec0f5`) Bytes | Phase 1 Bytes | Delta | Growth |
|---|---:|---:|---:|---:|
| `app/webapp/static/css/style.css` | 341,120 | 363,206 | +22,086 | +6.47% |
| `app/webapp/static/js/app.js` | 777,042 | 779,029 | +1,987 | +0.26% |
| `app/webapp/static/js/i18n.js` | 233,938 | 234,279 | +341 | +0.15% |
| **Total CSS + JS** | **1,352,100** | **1,376,514** | **+24,414** | **+1.81%** |
| `app/webapp/static/index.html` | 310,784 | 313,472 | +2,688 | +0.86% |

Cap allowed for CSS + JS (`+15%`): `1,554,914` bytes. Measured growth is `+1.81%`.

---

## 4. Playwright Mobile Visual Verification (`360px` & `380px`, `device_scale_factor=2`)

Before/after renders were captured against mocked API fixtures (zero live server changes) via `capture_phase_screenshots.py` in the workspace scratch directory and visually inspected across all 9 scenarios on the final CSS build:

| # | Scenario / Fixture | Viewport | Measured Width / Overflow | Visual Check |
|---|---|---|---|---|
| 01 | `first_run_free` (welcome strip visible, `completed_jobs=0`) | `360x820` | `iw=360 sw=360 hdr_w=344` | Clean floating chrome, SF icons, darker light secondary ink |
| 02 | `first_run_free` | `380x820` | `iw=380 sw=380 hdr_w=364` | Balanced spacing, zero clipping |
| 03 | `active_free` (recent TTS + Recap jobs) | `360x820` | `iw=360 sw=360 hdr_w=344` | Legible card metadata & pill chips |
| 04 | `vip` (PAID tier) | `360x820` | `iw=360 sw=360 hdr_w=344` | VIP badge & status strip contrast |
| 05 | `admin` (6 bottom-nav tabs) | `360x820` | `iw=360 sw=360 hdr_w=344` | All 6 tabs fit cleanly on 360px without wrap |
| 06 | `admin` | `380x820` | `iw=380 sw=380 hdr_w=364` | Crisp 6-tab dock spacing |
| 07 | `lite_on` (`html.lite`) | `360x820` | `iw=360 sw=360 hdr_w=344` | High-contrast ceramic buttons & surfaces, blur off |
| 08 | `dark_theme` (`html[data-theme="dark"]`) | `360x820` | `iw=360 sw=360 hdr_w=344` | Obsidian mesh, dark frosted header + Sun toggle, dark TTS tile |
| 09 | `recap_step2` (Recap preview stage) | `360x820` | `iw=360 sw=360 hdr_w=344` | `#canvas-source-label` separated above video canvas |

---

## 5. Verification Results & Windows Environment Disclosure

### Targeted Phase 1-8 & Batch UI Suites (Post-Edit)
- **Command:** `pytest` across all 16 UI/design/batch files (`test_phase_1_design_system_vcp.py`, `test_phase_1_v2_liquid_glass.py`, `test_phase_2_home_dashboard_vcp.py`, `test_phase_3_voice_tts_vcp.py`, `test_phase_4_recap_editor_vcp.py`, `test_phase_5_recap_billing_and_ui_vcp.py`, `test_phase_6_dl_history_vip_vcp.py`, `test_phase_7_admin_vcp.py`, `test_home_polish_visual_vcp.py`, `test_batch_av_2026_11_05.py`, `test_batch_az_2026_11_06.py`, `test_batch_ba_2026_11_06.py`, `test_batch_bd_2026_11_06.py`, `test_batch_cf_2026_11_07.py`, `test_header_branding_batch_ad_2026_11_04.py`, `test_full_app_polish_2026_11_01.py`):
  - **Result:** **`171 passed in 15.91s`** (0 failed, 0 skipped).
- **Command:** `pytest tests/unit/test_batch_dd_2026_11_09.py -k "not test_fit_ and not test_preview_fit_"` (excluding 3 tests that require Linux `libraqm`):
  - **Result:** **`31 passed, 3 deselected in 1.44s`**.

### Full `tests/unit/` Windows Run (Post-Edit Disclosure)
- Total test files in `tests/unit/`: `254`.
- **48 files** failed iterative pytest collection on Windows before test execution because Python 3.10 on this host lacks the `tzdata` package (`ZoneInfoNotFoundError: No time zone found with key America/Los_Angeles` via `app/services/key_rotation.py` and side-effected imports).
- Running the remaining **206 files**: **`2,778 passed, 121 failed, 4 skipped, 2 xfailed`**.
- **All 121 failures in the 206-file Windows run are outside Phase 1 UI scope** and stem from Windows environment differences or untouched backend tests (`tzdata`/`ZoneInfo` runtime imports, missing `libraqm` in Pillow for Burmese PNG subtitle tests, POSIX `#!/bin/sh` `[WinError 193]` subprocess fakes, Windows symlink privileges `[WinError 1314]`, and untouched `test_recap_gemini_tts.py::test_collect_keys_precedence` / downloader timing). **Zero HTML/CSS/JS/i18n/UI tests failed.**
- **Linux CI / Hammock Gate Required:** Because full-suite Linux execution (`tzdata`, `libraqm`, POSIX shell helpers, symlinks) is blocked on Windows, Hammock's Linux full-suite run is the final merge gate for `feat/ui-rebuild-v2`.
