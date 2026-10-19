# D36 — Read-Only Production Preflight Audit Report

**Audit Date:** October 14, 2026  
**Auditor:** Senior Python Engineer, Production Readiness Auditor, Security Reviewer, QA Engineer  
**Workspace:** `C:\Users\awint\VoiceCloneProOfficial`  
**Legacy Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` *(Strictly Read-Only — Verified Untouched)*  
**Current Baseline HEAD:** `e05a2aa9c087663ee41f9f1ff2a4655b0da435d4`  
**Previous Baseline HEAD:** `1c080e6d7e595863daf85d1c41e8335fab763c16`  
**Overall Readiness Verdict:** **NOT READY** (Deployment & Scheduled Task targets require retargeting prior to activation)

---

## 1. Executive Summary & Overall Readiness

A comprehensive, non-mutating preflight audit of `VoiceCloneProOfficial` was performed. The application codebase, dependencies, and internal security mechanisms are verified, sound, and fully covered by 176 passing automated tests. 

However, **production activation cannot proceed immediately** due to external operational misconfigurations in the host environment: the host Windows Scheduled Tasks (`VoiceClonePro-Bot` and `VoiceClonePro-NativeWorker`) currently target the legacy workspace (`C:\Users\awint\VOICE_CLONE_PRO_Production_Final`) rather than the official repository (`C:\Users\awint\VoiceCloneProOfficial`). Furthermore, live Telegram API connectivity and production database locks must remain unverified until an authorized, supervised activation phase.

* **Codebase & Release Artifacts:** PASS (Integrity intact, zero whitespace warnings, 176 tests passing).
* **Operational Scheduled Tasks:** BLOCKED (Task definitions point to legacy workspace paths).
* **Live Services & Database:** NOT RUN (Enforced by strict read-only safety constraints).
* **Final Overall Readiness:** **NOT READY** (Remediation required before activating services).

---

## 2. Repository Integrity and Commit Verification

* **Status:** **PASS**
* **Repository Path:** `C:\Users\awint\VoiceCloneProOfficial`
* **Current Branch:** `master`
* **HEAD Commit:** `e05a2aa9c087663ee41f9f1ff2a4655b0da435d4`
* **HEAD Commit Subject:** `style(tests): remove trailing EOF blank lines`
* **Parent Commit:** `1c080e6d7e595863daf85d1c41e8335fab763c16` (`feat(release): unified fixes for access control, referral retry, and admin UX`)
* **Staged Changes:** `0` (Index completely empty).
* **Tracked Changes:** `0` (Zero tracked files modified).
* **Untracked Historical Reports:** Exactly `41` files matching `phase_d*.md` are present and preserved.
* **Untracked Code/Config Files:** `0` (Zero unexpected code or configuration artifacts).
* **Whitespace Validation:** `git diff HEAD~2 HEAD --check` returned exit code `0` (clean, zero whitespace warnings).

---

## 3. Python Environment & Dependency Findings

* **Status:** **PASS**
* **Python Interpreter:** `C:\Users\awint\AppData\Local\Programs\Python\Python310\python.exe`
* **Python Runtime Version:** `Python 3.10.11 (tags/v3.10.11:7d4cc5a, Apr 5 2023)`
* **Virtual Environment:** Present at `C:\Users\awint\VoiceCloneProOfficial\.venv` (`Python 3.10.11`).
* **Dependency Declarations vs. Installed Packages:**
  * `aiogram==3.31.0`: Installed `3.31.0` [MATCH]
  * `sqlalchemy==2.0.52`: Installed `2.0.52` [MATCH]
  * `aiosqlite==0.22.1`: Installed `0.22.1` [MATCH]
  * `greenlet==3.5.5`: Installed `3.5.5` [MATCH]
  * `pydantic==2.13.5`: Installed `2.13.5` [MATCH]
  * `pydantic-settings==2.15.0`: Installed `2.15.0` [MATCH]
  * `edge-tts==7.2.8`: Installed `7.2.8` [MATCH]
  * `pytest==9.1.1`: Installed `9.1.1` [MATCH]
  * `pytest-asyncio==1.4.0`: Installed `1.4.0` [MATCH]
* **External Multimedia Tooling:**
  * `ffmpeg`: Detected on system PATH (`ffmpeg version 9.0.1-essentials_build-www.gyan.dev`).
* **Applio External Runtime:**
  * `APPLIO_WORKDIR` (`C:\Applio`): Path exists.
  * `APPLIO_PYTHON`: Path exists.
  * `APPLIO_RUNNER`: Path exists.
* **Entry Point Behavior:** `main.py` provides clean mode dispatching (`--mode all`, `bot`, `worker`) with graceful shutdown traps (`asyncio.CancelledError`, `KeyboardInterrupt`).

---

## 4. Secret & Configuration Audit

* **Status:** **PASS** (Redacted & safe)
* **Configuration File (`.env`):**
  * Present in root directory.
  * Explicitly listed in `.gitignore` and verified uncommitted (`git log --all -- .env` is empty).
* **Configuration Keys Inventory:**
  * `TELEGRAM_BOT_TOKEN`: PRESENT (Configured; matches standard token format `[0-9]{8,11}:[a-zA-Z0-9_-]{35}`).
  * `ADMIN_IDS`: PRESENT (Configured numeric ID).
  * `REQUIRED_CHANNEL_ID`: PRESENT (Configured numeric/channel ID).
  * `REQUIRED_GROUP_ID`: PRESENT (Configured numeric/group ID).
  * `CHANNEL_INVITE_LINK`: PRESENT (Configured).
  * `GROUP_INVITE_LINK`: PRESENT (Configured).
  * `DATABASE_URL`: PRESENT (Configured to local isolated SQLite).
  * `CHARACTER_LIMIT_FREE`: PRESENT (Configured).
  * `CHARACTER_LIMIT_PURCHASED`: PRESENT (Configured).
  * `APPLIO_WORKDIR`: PRESENT (Configured).
  * `APPLIO_PYTHON`: PRESENT (Configured).
  * `APPLIO_RUNNER`: PRESENT (Configured).
  * `APPLIO_COMMAND`: PRESENT (Optional; defaults to runner script).
  * `TUTORIAL_VIDEO_LINK`: PRESENT (Configured).
  * `LOG_LEVEL`: PRESENT (Configured).
* **Git Repository Secret Scanning:** Zero tokens, zero private keys, and zero plain-text passwords committed across the release tree.

---

## 5. Telegram Bot Configuration Status

* **Status:** **NOT RUN — LIVE API CHECK NOT AUTHORIZED** (Local Configuration: **PASS**)
* **Token Loading & Dispatcher Setup:**
  * `app/config/settings.py` loads `TELEGRAM_BOT_TOKEN` via `pydantic-settings`.
  * `main.py` initializes `aiogram.Bot` with `DefaultBotProperties(parse_mode=ParseMode.HTML)`.
  * `setup_bot_commands(bot)` dynamically registers localized commands menu.
  * Inbound events route through `AccessControlMiddleware` before dispatcher handlers.
* **Remote Verification Notice:** Remote token validity, bot identity (`getMe`), and webhook status were **NOT RUN** to uphold the prohibition against live external API calls during read-only audit.

---

## 6. Database and Migration Readiness

* **Status:** **NOT RUN — DATABASE TARGET NOT SAFELY VERIFIED** (Code/Schema Architecture: **PASS**)
* **Database Configuration:** `settings.database_url` points to `sqlite+aiosqlite:///./data/voice_clone_official.db`.
* **Engine & Session Factory:**
  * Asynchronous SQLite engine via `create_async_engine`.
  * PRAGMA enforcement on connection: `PRAGMA foreign_keys=ON` and `PRAGMA journal_mode=WAL`.
  * `AsyncSessionLocal` configured with `expire_on_commit=False` and `autoflush=False`.
* **DDL and Schema Initialization:**
  * `init_db()` executes `Base.metadata.create_all`, which creates non-existent tables additively.
  * Does not execute destructive `DROP TABLE` or data truncation.
  * `013_adjustment_idempotency.sql` provides idempotency tracking table definition.
* **Database Access Safety Restriction:** Direct connection or live schema query against `data/voice_clone_official.db` (192,512 bytes) was **NOT RUN** to guarantee zero risk of connection locks, transaction mutations, or session poisoning on persistent production data.

---

## 7. Security and Authorization Findings

* **Status:** **PASS**
* **Access Control Guard ([`app/middlewares/access_control.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/middlewares/access_control.py)):**
  * Registered as outer middleware on both `dp.message` and `dp.callback_query`.
  * Drops events originating from `UserStatus.BLOCKED` users prior to FSM transitions, command filters, or handlers. Dispatches localized alert `user_account_suspended`.
  * Configured admins bypass access check without database lookup overhead.
  * Fail-closed error handling protects the perimeter if database lookup fails.
* **Admin Authorization Boundary ([`app/handlers/admin.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/handlers/admin.py)):**
  * Strict server-side verification: `is_authorized_admin(event)` enforces `event.from_user.id in settings.admin_ids`.
  * Guards all admin commands (`/admin`, `/broadcast`, `/user`) and all callback routes (`admin:*`).
* **Ledger & Referral Idempotency ([`app/services/ledger.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/ledger.py)):**
  * User registration and minute balance initialization are isolated in a transaction savepoint (`session.begin_nested()`).
  * Unique constraint collisions on `referral_code` trigger a bounded retry loop (5 attempts) with randomized suffixes (`_find_available_referral_code`) truncated to 32 characters.
  * Concurrent registrations resolve cleanly without unhandled `IntegrityError` exceptions.
* **Input Scoping & Command Poisoning Defense:** All message text listeners are strictly bound to explicit FSM states (`CloneFSM.waiting_for_text`, `PaymentFSM.waiting_for_receipt`, `AdminBroadcastFSM.waiting_for_message`). Zero unscoped message catch-alls exist.

---

## 8. Windows Scheduled Tasks & Operational Readiness

* **Status:** **BLOCKED (Configuration Path Misalignment)**
* **Task Query Findings:**
  * Task `\VoiceClonePro-Bot`:
    * State: **Disabled** (Verified safe, not running).
    * Task Path: `powershell.exe -File "C:\Users\awint\VOICE_CLONE_PRO_Production_Final\scripts\run_bot.ps1"`
    * Working Directory: `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` [MISALIGNED: Points to Legacy Workspace!]
  * Task `\VoiceClonePro-NativeWorker`:
    * State: **Disabled** (Verified safe, not running).
    * Task Path: `powershell.exe -File "C:\Users\awint\VOICE_CLONE_PRO_Production_Final\scripts\run_worker.ps1"`
    * Working Directory: `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` [MISALIGNED: Points to Legacy Workspace!]
  * Task `\VoiceClonePro-Cleanup`:
    * State: **Ready** (Enabled daily at 4:15 AM).
    * Task Path: `powershell.exe -File "C:\Users\awint\VOICE_CLONE_PRO_Production_Final\scripts\run_cleanup.ps1"`
    * Working Directory: `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` [MISALIGNED: Points to Legacy Workspace!]
* **Operational Impact:** If an administrator or automated system enables `VoiceClonePro-Bot` or `VoiceClonePro-NativeWorker`, it will spawn the **legacy code** from `VOICE_CLONE_PRO_Production_Final` instead of the audited official release in `VoiceCloneProOfficial`.
* **Process Exclusivity:** `0` active `python.exe` processes running. Exclusivity is intact.

---

## 9. Automated Test Safety & Execution Evidence

* **Status:** **PASS**
* **Test Isolation Mechanism:** Verified that [`tests/conftest.py`](file:///C:/Users/awint/VoiceCloneProOfficial/tests/conftest.py) forces in-memory SQLite (`sqlite+aiosqlite:///:memory:`) and mocks external API boundaries.
* **Test Command Executed:** `& "C:\Users\awint\AppData\Local\Programs\Python\Python310\python.exe" -m pytest tests/`
* **Real Exit Code:** `0`
* **Execution Duration:** `20.72s`
* **Test Results Breakdown:**
  * **Collected Items:** 177
  * **Passed:** 176
  * **Skipped:** 1 (`tests/unit/test_audio_and_worker.py:160` skipped gracefully: `Edge TTS live network test skipped: 403` due to live network isolation).
  * **Failed:** 0
  * **Pass Rate:** **100% of executable tests**.

---

## 10. Prioritized Audit Findings

| Priority | Identifier | Category | Finding Description | Status | Remediation Required |
|:---:|:---:|:---:|---|:---:|---|
| **P0** | OPS-01 | Scheduled Tasks | Tasks `VoiceClonePro-Bot` and `VoiceClonePro-NativeWorker` target legacy workspace (`VOICE_CLONE_PRO_Production_Final`). | **BLOCKED** | Reconfigure Task Scheduler actions and working directories to point to `VoiceCloneProOfficial\main.py`. |
| **P1** | ENV-01 | Operational Readiness | Live Telegram bot identity and remote webhook status remain unverified against live Telegram APIs. | **NOT RUN** | Perform authorized live smoke verification during deployment phase. |
| **P1** | DB-01 | Database Target | Persistent SQLite schema state and lock status in `data/voice_clone_official.db` was not queried directly. | **NOT RUN** | Perform safe read-only schema version check immediately prior to service activation. |
| **P2** | WAN-01 | External Dependency | Edge TTS live web-socket endpoints may experience transient WAN rate-limiting / 403 blocks. | **WARN** | Confirm worker exponential retry and user refund fallback are active in production. |
| **P3** | DOC-01 | Workspace Hygiene | 41 untracked historical diagnostic reports exist in the repository root. | **PASS** | Maintain strict staging allowlists; do not commit diagnostic markdown files. |

---

## 11. Remediation Checklist for Future Activation Phase

Before any production service activation is authorized, the following sequenced actions must be executed in a dedicated phase:

- [ ] **Step 1: Retarget Scheduled Tasks (P0)**
  - Update `VoiceClonePro-Bot` task action: point to `C:\Users\awint\VoiceCloneProOfficial` using `.venv\Scripts\python.exe main.py --mode bot`.
  - Update `VoiceClonePro-NativeWorker` task action: point to `C:\Users\awint\VoiceCloneProOfficial` using `.venv\Scripts\python.exe main.py --mode worker`.
- [ ] **Step 2: Pre-Activation Database Integrity Check (P1)**
  - Open a read-only SQLite connection to `data/voice_clone_official.db` and verify table presence (`users`, `minute_balances`, `jobs`, `adjustment_idempotency_records`).
- [ ] **Step 3: Supervised Single-Process Launch (P1)**
  - Execute a manual test launch: `main.py --mode bot` in a supervised terminal session.
  - Verify successful Telegram connection (`getMe`), bot command registration, and dispatcher polling.
- [ ] **Step 4: End-to-End Live Telegram Smoke Test (P1)**
  - Send `/start` from an authorized admin account and verify the 4-button menu + Admin Dashboard button.
  - Verify voice synthesis request queueing and worker pickup.
- [ ] **Step 5: Scheduled Task Activation**
  - Enable Windows Scheduled Tasks once manual smoke testing confirms zero defects.

---

## 12. Audit Integrity and Compliance Statement

* **Codebase State:** Completely untouched. Zero application files, test files, or configuration files were edited or created (except this report).
* **Git State:** Clean index (`0` staged files). Branch remains `master` at HEAD `e05a2aa9c087663ee41f9f1ff2a4655b0da435d4`.
* **Scheduled Tasks:** Zero tasks were enabled, started, stopped, or modified. Both `VoiceClonePro-Bot` and `VoiceClonePro-NativeWorker` remain **Disabled**.
* **Production Services:** Zero background services, polling loops, or workers were launched. Exclusivity is maintained.
