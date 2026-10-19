# Phase D13-A Evidence Report: Production Startup & Broadcast Schema Preflight

**Phase:** D13-A — Production Startup & Broadcast Schema Preflight  
**Workspace:** `C:\Users\awint\VoiceCloneProOfficial`  
**Legacy Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` (Strictly Read-Only / Untouched)  
**Execution Timestamp:** 2026-10-12T18:13:30+06:30  
**Auditor / Roles:** Senior Python Engineer, Database Architect & QA Auditor  

---

## 1. Executive Summary & Verdict

### Final Preflight Verdict: **PASS WITH CONDITIONS**

A comprehensive, non-mutating preflight audit of the application startup lifecycle, database initialization mechanics, broadcast schema integration, operational safety boundaries, and full test suite regression was performed.

### Verdict Rationale
1. **Startup Database Behavior (PASS):**
   - Application startup in [`main.py`](file:///C:/Users/awint/VoiceCloneProOfficial/main.py#L24-L65) calls `await init_db()` as its very first action (line 26), strictly *before* stale job recovery (lines 40-50), worker loop activation (line 52), and Telegram bot polling (line 64).
   - Module-level imports in [`main.py`](file:///C:/Users/awint/VoiceCloneProOfficial/main.py#L1-L15) load [`app/handlers/__init__.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/handlers/__init__.py), [`app/handlers/admin.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/handlers/admin.py), and [`app/services/broadcast.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/broadcast.py), which in turn imports [`app/models/schema.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/models/schema.py).
   - Consequently, all 14 models—including the newly added [`BroadcastCampaign`](file:///C:/Users/awint/VoiceCloneProOfficial/app/models/schema.py#L374-L399) and [`BroadcastDelivery`](file:///C:/Users/awint/VoiceCloneProOfficial/app/models/schema.py#L401-L421)—are fully registered on `Base.metadata` when `await conn.run_sync(Base.metadata.create_all)` executes in [`init_db()`](file:///C:/Users/awint/VoiceCloneProOfficial/app/db/session.py#L40-L49).
   - SQLAlchemy's `create_all(checkfirst=True)` automatically detects that `broadcast_campaigns` and `broadcast_deliveries` do not exist in the database and creates them with all required tables, columns, constraints, foreign keys, and indexes. It leaves all 12 pre-existing tables and their existing rows completely untouched.
2. **Isolated Schema Experiment (PASS):**
   - An isolated SQLite database was provisioned in a temporary directory containing only the 12 pre-existing production tables.
   - `Base.metadata.create_all` was executed against this database. Both `broadcast_campaigns` and `broadcast_deliveries` were created successfully.
   - Columns, unique constraints, foreign keys (`broadcast_deliveries.campaign_id -> broadcast_campaigns.id`), and unique composite constraint `(campaign_id, user_id)` were verified via SQLite PRAGMA commands.
   - AsyncSession insert, update, and enum round-trip queries succeeded. `PRAGMA integrity_check` and `PRAGMA foreign_key_check` both returned clean (0 errors).
   - The production database was 100% isolated: file size, checksum, and `mtime` remained completely unchanged.
3. **Operational Broadcast Safety (PASS):**
   - End-to-end tracing confirms:
     - FSM token matching + immediate `await state.clear()` prevents double-clicks and replay attacks.
     - Database unique constraint on `confirmation_token` provides durable idempotency.
     - Full Telegram `retry_after` delay is honored (up to a 300s safety ceiling) with bounded retries (`max_retries = 2`).
     - Definitive `TelegramForbiddenError` automatically marks recipients as `BLOCKED`.
     - Intermediate delivery commits ensure crash-resilient campaign state.
4. **Regression Verification (PASS):**
   - Full test suite: **104 passed in 17.87s** (0 failed, 0 errors, 0 skipped, 0 warnings).
5. **Production Database Integrity (PASS):**
   - Read-only inspection of [`data/voice_clone_official.db`](file:///C:/Users/awint/VoiceCloneProOfficial/data/voice_clone_official.db): `PRAGMA integrity_check` returned `ok`. All existing table counts are unchanged (1 user, 3 jobs, 2 voice models, 4 audit logs, 1 minute balance).

---

## 2. Conditions Required Before Live Smoke Test

Before authorizing live bot execution in Stage 2, the following conditions must be strictly satisfied:

| Condition # | Requirement | Implementation & Safety Rule |
| :--- | :--- | :--- |
| **C-1** | **WAL-Safe SQLite Backup** | Before booting the bot, create a verified backup of [`data/voice_clone_official.db`](file:///C:/Users/awint/VoiceCloneProOfficial/data/voice_clone_official.db) using SQLite's online backup API (`sqlite3.Connection.backup`). Verify backup integrity via `PRAGMA integrity_check`. |
| **C-2** | **Single-Instance Polling** | Verify 0 Python bot processes are running before startup. Launch exactly one instance with `python main.py --mode all`. Never run multiple bot pollers concurrently. |
| **C-3** | **Admin-Only Scope Gate** | Restrict the live smoke test exclusively to the configured admin Telegram ID (`settings.admin_telegram_id`). Zero broadcast dispatches to external or non-admin users. |
| **C-4** | **Delivery Ambiguity Acceptance** | Operational recognition that external HTTP messaging lacks distributed two-phase commit: if Telegram receives a message but the process crashes before SQLite commits `SENT`, the recipient remains `PENDING` and will be re-sent upon campaign resume. |

---

## 3. Startup Database Behavior & Code Path Analysis

### 3.1 Startup Sequence in [`main.py`](file:///C:/Users/awint/VoiceCloneProOfficial/main.py)
Tracing `run_bot_and_worker(mode)`:
- **Line 25-26:** `logger.info("Initializing database...")` followed by `await init_db()`.
- **Line 28-33:** Telegram `Bot` instance initialized with HTML parse mode.
- **Line 39-50:** Boot recovery sweep via `recover_stale_running_jobs(session=session, bot=bot)` cleans up any orphaned jobs left in `RUNNING` status from prior process terminations.
- **Line 51-53:** Audio worker loop `run_worker_loop(bot=bot)` launched as an `asyncio.create_task`.
- **Line 55-65:** Aiogram Dispatcher configured via `setup_handlers(dp)` and `dp.start_polling(bot)` launched as an `asyncio.create_task`.

**Finding:** Database table initialization strictly precedes bot polling and worker task execution. The database is fully initialized and consistent before any Telegram updates or audio jobs can be processed.

### 3.2 Table Creation Mechanics in [`app/db/session.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/db/session.py)
Lines 40-49 of `init_db()`:
```python
async def init_db() -> None:
    """Create all tables in development/test environments."""
    if settings.storage_root:
        settings.storage_root.mkdir(parents=True, exist_ok=True)
    if settings.data_root:
        settings.data_root.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
```
- `engine.begin()` starts a transaction and sets SQLite connection pragmas: `PRAGMA foreign_keys=ON` and `PRAGMA journal_mode=WAL` (lines 28-29).
- `Base.metadata.create_all` issues `CREATE TABLE IF NOT EXISTS` statements for any table present in SQLAlchemy metadata that does not yet exist in SQLite `sqlite_master`.
- Existing tables (`users`, `jobs`, `voice_models`, etc.) are detected and skipped; their schemas and data are completely preserved.

### 3.3 Metadata Registration Trace
When `main.py` is loaded:
1. `main.py` line 14: `from app.handlers import setup_handlers`
2. `app/handlers/__init__.py` line 2: `from app.handlers.admin import admin_router`
3. `app/handlers/admin.py` line 20: `from app.services.broadcast import BroadcastResult, create_or_get_broadcast_campaign, deliver_broadcast, get_broadcast_recipient_ids`
4. `app/services/broadcast.py` line 25: `from app.models.schema import BroadcastCampaign, BroadcastDelivery, User`
5. Importing `app.models.schema` registers all 14 entity classes onto `Base.metadata.tables`:
   - `users`
   - `minute_balances`
   - `minute_ledger_events`
   - `welcome_bonus_awards`
   - `referrals`
   - `referral_rewards`
   - `pricing_plans`
   - `payment_requests`
   - `voice_models`
   - `jobs`
   - `audit_logs`
   - `bot_settings`
   - `broadcast_campaigns` *(new)*
   - `broadcast_deliveries` *(new)*

**Finding:** Both broadcast models are guaranteed to be registered in `Base.metadata` when `init_db()` runs during `main.py` startup.

---

## 4. Isolated Schema Experiment Results

To prove that `init_db()` safely migrates an existing production database containing only the 12 pre-D12 tables, an isolated automated experiment was executed in a temporary directory (`C:\Users\awint\AppData\Local\Temp\d13a_isolated_*`).

### Experiment Steps & Observed Outputs:
1. **Pre-Existing State Setup:**
   - Extracted schema DDL from production database for all tables except broadcast tables.
   - Initialized temporary SQLite database with only the 12 existing tables:
     `['audit_logs', 'bot_settings', 'jobs', 'minute_balances', 'minute_ledger_events', 'payment_requests', 'pricing_plans', 'referral_rewards', 'referrals', 'users', 'voice_models', 'welcome_bonus_awards']`
   - Confirmed `broadcast_campaigns` and `broadcast_deliveries` were absent.
2. **Executing `Base.metadata.create_all`:**
   - Ran `create_all` against the temporary database.
   - Verified new table list: all 14 tables now present.
3. **Schema Verification on `broadcast_campaigns`:**
   - Columns: `id`, `admin_id`, `confirmation_token`, `message_text`, `status`, `total_recipients`, `sent_count`, `failed_count`, `skipped_count`, `duration_seconds`, `created_at`, `started_at`, `finished_at`.
   - Indexes: `ix_broadcast_campaigns_confirmation_token` (unique), primary key index on `id`.
4. **Schema Verification on `broadcast_deliveries`:**
   - Columns: `id`, `campaign_id`, `user_id`, `status`, `attempts`, `error_message`, `sent_at`.
   - Foreign Keys: `campaign_id` references `broadcast_campaigns(id)` with `ON DELETE CASCADE`.
   - Constraints: Composite unique constraint `(campaign_id, user_id)` verified via `sqlite_autoindex_broadcast_deliveries_1`.
5. **Persistence & Enum Round-Trip:**
   - Inserted active `User`, `BroadcastCampaign` with `BroadcastCampaignStatus.PENDING`, and `BroadcastDelivery` with `BroadcastDeliveryStatus.PENDING`.
   - Loaded campaign with relationship loading (`selectinload`).
   - Updated statuses to `BroadcastCampaignStatus.COMPLETED` and `BroadcastDeliveryStatus.SENT`. Committed and queried back. All status values and relationships persisted cleanly.
6. **Integrity & Foreign Key Checks:**
   - `PRAGMA integrity_check`: `ok`
   - `PRAGMA foreign_key_check`: 0 errors
7. **Cleanup & Isolation Verification:**
   - Temporary directory unlinked and removed.
   - Production database [`data/voice_clone_official.db`](file:///C:/Users/awint/VoiceCloneProOfficial/data/voice_clone_official.db) verified untouched: `mtime=1791797437.3568804` (identical before and after experiment).

---

## 5. Broadcast Operational Safety & Risk Trace

### 5.1 Campaign Lifecycle & Confirmation
- **Flow:** `/admin` -> `admin:broadcast` -> prompt message text -> enter text -> `handle_admin_broadcast_message_input` validates length and formats preview -> generates nonce token (`uuid.uuid4().hex[:16]`).
- **Confirmation:** Handled in [`handle_admin_broadcast_confirm`](file:///C:/Users/awint/VoiceCloneProOfficial/app/handlers/admin.py#L754-L858).
  - Admin identity verified against `settings.admin_telegram_id`.
  - Nonce token verified against FSM state.
  - **Immediate FSM State Invalidation:** `await state.clear()` is called on line 783 *before* database creation or network requests. Any subsequent button clicks or duplicate network packets encounter an expired state and are rejected with an alert.
  - **Durable Database Creation:** [`create_or_get_broadcast_campaign`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/broadcast.py#L63-L106) creates the `BroadcastCampaign` row and pre-populates all recipient rows in `BroadcastDelivery` with status `PENDING`.

### 5.2 Rate Limiting, Backoff & Error Isolation
- **Send Interval:** Default interval is 0.1s (10 messages/sec), providing 3x headroom beneath Telegram's global 30 msg/sec limit.
- **Flood Wait Backoff:** When Telegram raises `TelegramRetryAfter(retry_after=N)`:
  - Full requested delay is respected (`sleep_duration = float(e.retry_after)`), removing the prior 5.0s cap.
  - Safety ceiling of 300.0s (`max_retry_after`) prevents indefinite hanging on excessive server delays.
  - Bounded retry budget: `max_retries = 2`. When budget is exhausted, the delivery is marked `FAILED` with an error message, without unnecessary sleeping.
- **Definitive User Block Handling:** Upon `TelegramForbiddenError`, [`mark_user_blocked`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/broadcast.py#L52-L61) updates `User.status = 'BLOCKED'` in the database. Future campaigns skip this user automatically via [`get_broadcast_recipient_ids`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/broadcast.py#L41-L49).
- **Transient Error Isolation:** `TelegramAPIError` and network exceptions mark the recipient `FAILED` without mutating user status.

### 5.3 Crash Recovery & Resume
- **Resume Mechanism:** [`resume_broadcast_campaign`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/broadcast.py#L326-L372) queries remaining `BroadcastDelivery` rows where `status == 'PENDING'`. Already sent (`SENT`), failed (`FAILED`), or skipped (`SKIPPED`) rows are never re-processed.
- **Intermediate Commits:** Line 288 of `deliver_broadcast` commits progress after each processed batch or recipient, ensuring that partial progress is persistently saved even if the host machine experiences a sudden power loss.

### 5.4 Delivery Ambiguity Analysis
- **External API Boundary Risk:** When Telegram accepts a message via HTTP 200, but the local host crashes before SQLite commits `delivery.status = 'SENT'`, the delivery row remains `PENDING` on disk. Upon resume, this recipient will receive a duplicate message.
- **Conclusion:** As documented in the architecture notes, this is an unavoidable characteristic of third-party HTTP APIs that do not support two-phase distributed transactions. The durable delivery tracker minimizes the window of vulnerability to milliseconds per recipient.

---

## 6. Regression Verification Results

The complete automated test suite was executed against the official codebase:

- **Command:** `.venv\Scripts\python.exe -m pytest -v`
- **Working Directory:** `C:\Users\awint\VoiceCloneProOfficial`
- **Execution Time:** 17.87 seconds
- **Test Summary:**
  - Total items collected: 104
  - **Passed: 104 (100%)**
  - **Failed: 0**
  - **Errors: 0**
  - **Warnings: 0**
  - **Skipped: 0**

### Test Breakdown by Subsystem:
- `test_admin_handlers.py`: 11 passed (admin dashboard, pricing, tutorial, HTML escaping, empty DB safety)
- `test_admin_unlimited_access.py`: 14 passed (admin bypass, zero balance, unlimited quota, spoofing prevention)
- `test_applio_settings_regression.py`: 4 passed (Applio CLI runner resolution and fallback)
- `test_audio_and_worker.py`: 4 passed (Edge-TTS, pitch mapping, worker lifecycle, queue position)
- `test_broadcast.py`: 18 passed (preview, cancellation, double-click, retry-after backoff, durable resume, blocked sync)
- `test_concurrency.py`: 2 passed (welcome bonus and referral reward concurrent locks)
- `test_crash_recovery.py`: 9 passed (stale job sweep, idempotent refund, temp cleanup, recovery notification)
- `test_i18n.py`: 8 passed (Burmese/English parity, stepper progress, caption escaping, admin localization)
- `test_ledger.py`: 4 passed (ledger balance transitions, referral attribution, audit trail)
- `test_model_catalog_and_scan.py`: 2 passed (voice model discovery, artifact pairing, admin approval)
- `test_payments.py`: 2 passed (pricing plan listing, payment approval/rejection lifecycle)
- `test_progress_ui.py`: 11 passed (stepper progress bar, ticker task lifecycle, HTML escaping)
- `test_queue_concurrency.py`: 4 passed (worker job claiming concurrency, idempotent debit/refund)
- `test_safe_model_activation.py`: 4 passed (PTH verification, safe activation lifecycle, scan idempotency)
- `test_telegram_ux_and_auth.py`: 8 passed (two-button start menu, admin dashboard button, channel gates, help/lang)

---

## 7. Production Database Read-Only Integrity Check

Inspected [`data/voice_clone_official.db`](file:///C:/Users/awint/VoiceCloneProOfficial/data/voice_clone_official.db) strictly via SQLite URI read-only connection (`?mode=ro`):

| Property / Table | Value / Count | Integrity Check |
| :--- | :--- | :--- |
| **PRAGMA integrity_check** | `ok` | PASS |
| **`users`** | 1 record | Preserved from D8 |
| **`jobs`** | 3 records | Preserved from D8 |
| **`voice_models`** | 2 records | Preserved from D8 |
| **`audit_logs`** | 4 records | Preserved from D8 |
| **`minute_balances`** | 1 record | Preserved from D8 |
| **`minute_ledger_events`** | 0 records | Intact |
| **`payment_requests`** | 0 records | Intact |
| **`pricing_plans`** | 0 records | Intact |
| **`referrals`** | 0 records | Intact |
| **`referral_rewards`** | 0 records | Intact |
| **`welcome_bonus_awards`**| 0 records | Intact |
| **`bot_settings`** | 0 records | Intact |
| **`broadcast_campaigns`** | Table absent (will auto-create on boot) | Expected |
| **`broadcast_deliveries`** | Table absent (will auto-create on boot) | Expected |

---

## 8. Repository & Workspace State

- **Official Workspace:** `C:\Users\awint\VoiceCloneProOfficial`
- **Git Branch:** `master`
- **Git HEAD:** Initial working tree, 0 commits
- **Working Tree:** Intact untracked application files preserved; no files modified, deleted, or staged.
- **Legacy Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` verified completely untouched (strictly read-only).
- **Process State:** Zero running Python or bot processes.

---

## 9. Final Recommendation for Stage 2 (Live Smoke Test)

Based on the evidence from Phase D13-A:

1. **Authorization Recommendation:**  
   The application is **CLEARED TO PROCEED** to a strictly scoped, admin-only live smoke test (**Phase D13-B**).
2. **Mandatory Execution Gate Sequence for D13-B:**
   - **Step 1:** Execute online WAL-safe backup of `data/voice_clone_official.db` to a timestamped backup file using Python's SQLite backup API. Run `PRAGMA integrity_check` on the backup.
   - **Step 2:** Start a single official bot instance: `python main.py --mode all`.
   - **Step 3:** Confirm via logs that `init_db()` created `broadcast_campaigns` and `broadcast_deliveries`.
   - **Step 4:** Execute the live test strictly using the configured admin Telegram ID:
     - Verify `/start` and 2-button main menu (`Voice Studio` + `Admin Dashboard`).
     - Open `Admin Dashboard` -> Click `Broadcast`.
     - Compose a short test broadcast message.
     - Review preview with recipient count (1 recipient = admin).
     - Confirm broadcast and verify delivery of exactly one message to the admin.
     - Verify dashboard metrics update properly.
     - Submit one voice generation job in Voice Studio to verify end-to-end audio pipeline.
   - **Step 5:** Terminate the bot cleanly with SIGINT / Ctrl+C and verify database post-test counts and integrity.
