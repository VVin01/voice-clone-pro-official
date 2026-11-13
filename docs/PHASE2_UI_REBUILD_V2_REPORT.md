# PHASE 2 REPORT — FULL UI REBUILD V2 (Home 4-Zone IA + Champagne Pearl VIP + Unified Queue Cards)

**Branch:** `feat/ui-rebuild-v2` (from `main` `8d3248c`, PR #187 Phase 1 merged)
**Cache Token:** `20261113_r144` (bumped from `20261113_r143`)
**Date:** 2026-11-13

---

## Summary of Changes

1. **Zone A — Unified Identity Hero + 3-Col Balance Trio (`#home-hero-card`, `#home-status-strip`, `#home-free-card`, `#home-welcome-strip`)**
   - Unified greeting, tagline (`home_hero_tagline`), live readiness indicator (`home_status_ready`), clickable tier badge (`FREE` / `VIP` / `ADMIN`), and 3-column bento balance tiles (`TTS`, `Voice`, `Recap`) with crisp SVGs and period chips inside `#home-hero-card`.
   - `paintHomeStatusStrip(data)` sets `data-tier` (`free` / `vip` / `admin`) on `#home-hero-card` and `#tab-home` so paid VIP & Admin users receive the Champagne Pearl gold hero card automatically in light/dark/lite themes.
   - `#home-welcome-strip` stays directly inside Zone A with the exact `class="welcome-strip hidden"` and first-run CTAs intact.
   - Fixed stray closing brace `}` before `#tab-home #home-hero-card #home-tier-badge.vcp-badge-info` in `app/webapp/static/css/style.css` so the `FREE` tier pill renders with crisp blue contrast in both Light and Lite modes.

2. **Zone B — Studio Launcher Bento (`#home-start-card`)**
   - Prominent hero launcher tile `#home-start-recap-tile` (`Recap Maker`) above twin 2-column cards (`Voice Studio`, `TTS Studio`), with SF-style vector icon discs, `44px+` touch targets, and crisp wrapping in Myanmar and English at `360px` and `380px`.

3. **Zone C — Recent Projects Designed States (`#home-recent-projects-card`)**
   - Designed shimmer skeleton (`.vcp-skel-row`), empty frosted glass state with vector medallion (`.vcp-empty-glass`), network error glass state with retry CTA (`.vcp-error-glass`), and SF-style SVG job type icons in `renderHomeRecentProjects()`.

4. **Zone D — Grouped Tools List, Help Hub & Refer (`#home-tools-card`, `#home-help-entry-card`, `#home-refer-card`)**
   - `#home-tools-card` rendered as a clean iOS-style grouped list (`Downloader`, `History`), paired with `#home-help-entry-card` accordion entry and `#home-refer-card` / `.home-support-card`.

5. **Unified Non-Overlapping Queue / Job Card System (Phase 2 MUST-FIX)**
   - Standardized `.vcp-job-card` + `.vcp-job-progress` + dedicated `.vcp-job-footer-row` across ALL 4 queue surfaces (`renderHomeRecentProjects()`, `hubHistoryRowHtml()`, `liveJobsSheetRowHtml()`, `loadAdminQueue()`), separating badges/expiry chips (`⚠️ မကြာမီ သက်တမ်းကုန် ...`) into full-width wrap-safe rows above a hairline-divided action footer so buttons NEVER cover chips at `360px` or `380px` in any job state (`running` / `done` / `expiring` / `failed`).

---

## Guardrails & Asset Checks (`v2_audit.py`)

- **ID Parity vs `main` (`8d3248c`):** `missing_ids=[]`
- **Onclick Parity vs `main` (`8d3248c`):** `missing_onclick=[]`
- **ID Parity vs `b4ec0f5a` (`r142`):** `missing_ids=[]`, `missing_onclick=[]`
- **CSS + JS Asset Weight (LF normalized):** `1,407,666` bytes (`+4.11%` vs `b4ec0f5a` `1,352,100` bytes; well below `+15%` cap `1,554,914`)
  - `css/style.css`: `389,174` bytes
  - `js/app.js`: `783,065` bytes
  - `js/i18n.js`: `235,427` bytes
- **Non-chrome `backdrop-filter` in V2 CSS:** `[]` (none in scrollable cards)
- **CSS brace balance check:** `Final depth: 0` (zero negative depths)
- **Syntax / Compilation checks:** `node --check app/webapp/static/js/app.js`, `node --check app/webapp/static/js/i18n.js`, `python -m compileall -q tests/unit` — all exited `0`.

---

## Verification & Visual Walk (Windows Host Limits Noted)

- **V2 Structural Suites (`test_phase_1_v2_liquid_glass.py`, `test_phase_2_v2_home.py`):** `17 passed in 3.07s`
- **Filtered Windows UI/Static Pytest Suite:** `1,450 passed in 262.49s` (0 failures)
- **Broader Windows Environment Limitations (Hammock Linux Gate Covers Full Suite):**
  - Python 3.10 on Windows lacks `tzdata` (blocking `ZoneInfo("America/Los_Angeles")` imports in backend/admin handler modules, e.g., `test_batch_bz_2026_11_07.py`, `test_batch_cb_2026_11_07.py`, `test_admin_*.py`) and Pillow lacks `libraqm` (blocking Burmese subtitle PNG shaping tests).
  - Updated cache-token pins across 82 test files (`20261113_r143` -> `20261113_r144`) including split-literal pin in `tests/unit/test_batch_cb_2026_11_07.py`; verified zero stale `r143` token pins remain outside V2 negative assertions and verified all test `.py` files compile cleanly via `compileall`.
- **Playwright Chromium Mobile Walk (`phase2_after/`, 15 cases at `360x820` & `380x820`, light/dark/lite, first-run FREE / active FREE / VIP / Admin / scrolled Zone C & D / History / Live Jobs Sheet / Admin Queue):**
  - Zero console `pageerror`s across all 15 captures.
  - Zero horizontal overflow across all 15 captures (`scrollWidth == innerWidth` at `360` and `380`).
  - Visually confirmed crisp `FREE` / `VIP` / `ADMIN` hero pills, Champagne Pearl gold VIP/Admin card, and zero overlap between status/retention badges and download/action buttons on Home Recent, History, Live Jobs Sheet, and Admin Queue.
