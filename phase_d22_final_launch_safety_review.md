# Phase D22 — Final Launch Safety Review (STRICT READ-ONLY)

**Target Workspace:** `C:\Users\awint\VoiceCloneProOfficial`  
**Legacy Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` (*Strictly Read-Only — Untouched*)  
**Audit Timestamp:** 2026-10-13T04:20:00+06:30  
**Audit Mode:** STRICT READ-ONLY STATIC & RUNTIME SAFETY INSPECTION  

---

## 1. Final Verdict

### `NOT READY — BLOCKERS REMAIN`

**Primary Blocker:** Active background supervisor loops in Windows Task Scheduler.  
The Windows Task Scheduler tasks `VoiceClonePro-Bot` and `VoiceClonePro-NativeWorker` are currently running in the background (PowerShell supervisor PIDs 18284 and 18380). Every 180 seconds, the legacy launcher scripts attempt to boot the legacy `bot.py` and `worker.py` from `C:\Users\awint\VOICE_CLONE_PRO_Production_Final`. Because Phase D20 aligned the Official bot token to match the legacy bot token (`8631167018:...`), launching VoiceClonePro Official under this machine state will cause periodic **HTTP 409 Conflict** (`TelegramConflictError: terminated by other getUpdates request`) and potential update interception.

Before VoiceClonePro Official can be safely launched, the operator must explicitly disable and stop these legacy scheduled tasks.

---

## 2. Proposed Launch Command & Verified Call Path

### 2.1 Proposed Launch Command
```powershell
cd C:\Users\awint\VoiceCloneProOfficial
& "C:\Users\awint\AppData\Local\Programs\Python\Python310\python.exe" main.py --mode all
```

### 2.2 Verified Execution Call Path
1. **Entrypoint & CLI Argument Parsing:**
   - [`main.py#L86-L95`](file:///C:/Users/awint/VoiceCloneProOfficial/main.py#L86-L95): `main()` parses `--mode all` (choices: `all`, `bot`, `worker`). Default is `all`.
   - [`main.py#L97`](file:///C:/Users/awint/VoiceCloneProOfficial/main.py#L97): `asyncio.run(run_bot_and_worker(mode="all"))`.

2. **Database Initialization:**
   - [`main.py#L27`](file:///C:/Users/awint/VoiceCloneProOfficial/main.py#L27): `await init_db()`
   - [`app/db/session.py#L40-L49`](file:///C:/Users/awint/VoiceCloneProOfficial/app/db/session.py#L40-L49): `init_db()` ensures directory paths exist (`storage/`, `data/`) and executes `Base.metadata.create_all` via SQLAlchemy async engine.
   - [`app/db/session.py#L24-L30`](file:///C:/Users/awint/VoiceCloneProOfficial/app/db/session.py#L24-L30): Event listener configures `PRAGMA foreign_keys=ON` and `PRAGMA journal_mode=WAL` upon connection.

3. **Telegram Bot Initialization (Local):**
   - [`main.py#L30-L34`](file:///C:/Users/awint/VoiceCloneProOfficial/main.py#L30-L34): Instantiates `Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))`. No network I/O occurs at this step.

4. **Boot Stale Job Sweep:**
   - [`main.py#L45-L48`](file:///C:/Users/awint/VoiceCloneProOfficial/main.py#L45-L48): Opens `AsyncSessionLocal()` and calls `recover_stale_running_jobs(session, bot)`.
   - [`app/services/recovery.py#L51-L60`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/recovery.py#L51-L60): Queries `Job.status == 'RUNNING' AND Job.started_at <= (now - 600s)`. (Evaluates to 0 candidates in current database).

5. **Audio Worker Task Launch:**
   - [`main.py#L53`](file:///C:/Users/awint/VoiceCloneProOfficial/main.py#L53): `worker_task = asyncio.create_task(run_worker_loop(bot=bot))`.
   - [`app/worker.py#L198-L207`](file:///C:/Users/awint/VoiceCloneProOfficial/app/worker.py#L198-L207): Performs initial worker recovery sweep.
   - [`app/worker.py#L208-L215`](file:///C:/Users/awint/VoiceCloneProOfficial/app/worker.py#L208-L215): Enters infinite loop polling `claim_and_process_next_job(session, bot=bot)` with a 2.0s sleep interval when queue is empty.

6. **Telegram Native Commands Registration & Handlers Setup:**
   - [`main.py#L63`](file:///C:/Users/awint/VoiceCloneProOfficial/main.py#L63): `await setup_bot_commands(bot)`
   - [`app/ui/commands.py#L27-L44`](file:///C:/Users/awint/VoiceCloneProOfficial/app/ui/commands.py#L27-L44): Calls `await bot.set_my_commands(DEFAULT_COMMANDS, scope=BotCommandScopeDefault())`. Only `/start` is registered.
   - [`main.py#L64-L65`](file:///C:/Users/awint/VoiceCloneProOfficial/main.py#L64-L65): Instantiates `Dispatcher()` and includes routers via `setup_handlers(dp)` ([`app/handlers/__init__.py#L9-L14`](file:///C:/Users/awint/VoiceCloneProOfficial/app/handlers/__init__.py#L9-L14)).

7. **Telegram Polling Task Launch:**
   - [`main.py#L67`](file:///C:/Users/awint/VoiceCloneProOfficial/main.py#L67): `bot_task = asyncio.create_task(dp.start_polling(bot))`.
   - Triggers Aiogram polling loop, making outbound HTTP requests to Telegram Bot API (`getUpdates`).

8. **Cooperative Multitasking & Wait:**
   - [`main.py#L75`](file:///C:/Users/awint/VoiceCloneProOfficial/main.py#L75): `await asyncio.gather(*tasks)` keeps process running until interrupt or cancellation.

---

## 3. Detailed Safety Findings & Evidence

### Finding 1: Legacy Windows Scheduled Tasks Actively Looping (CRITICAL BLOCKER)
- **File / Component:** Windows Task Scheduler (`\VoiceClonePro-Bot`, `\VoiceClonePro-NativeWorker`, `\VoiceClonePro-Cleanup`)
- **Observed Behavior:**
  - `VoiceClonePro-Bot` is in `State: Running` with Action `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\awint\VOICE_CLONE_PRO_Production_Final\scripts\run_bot.ps1"`.
  - `VoiceClonePro-NativeWorker` is in `State: Running` with Action `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\awint\VOICE_CLONE_PRO_Production_Final\scripts\run_worker.ps1"`.
  - Process inspection confirms active PowerShell processes: **PID 18284** and **PID 18380** (spawned by Task Scheduler host `svchost.exe` PID 2440).
  - Inspection of `C:\Users\awint\VOICE_CLONE_PRO_Production_Final\logs\bot_launcher.log` demonstrates that every 180 seconds, `run_bot.ps1` times out waiting for the stopped PostgreSQL container, logs `WARN PostgreSQL not ready after 180s... continuing anyway`, and executes `"C:\Users\awint\VOICE_CLONE_PRO_Production_Final\.venv\Scripts\python.exe" "C:\Users\awint\VOICE_CLONE_PRO_Production_Final\bot.py"` (most recent: restart #2069 at 04:15:08).
- **Inferred vs Observed:**
  - *Phase D21 Inference:* CHK-11 assumed process exclusivity passed because a point-in-time check saw 0 Python processes.
  - *Phase D22 Observed Reality:* The PowerShell supervisors run 24/7. When legacy `bot.py` is invoked during its retry loop, it uses the identical Telegram bot token (`8631167018:...`), causing direct polling conflict with any running instance of VoiceClonePro Official.
- **Verification Status:** **VERIFIED**
- **Severity:** **CRITICAL**
- **Safe Recommendation (For Operator Action Prior to Launch):**
  Operator must run in an elevated PowerShell terminal:
  ```powershell
  Stop-ScheduledTask -TaskName "VoiceClonePro-Bot", "VoiceClonePro-NativeWorker" -ErrorAction SilentlyContinue
  Disable-ScheduledTask -TaskName "VoiceClonePro-Bot", "VoiceClonePro-NativeWorker", "VoiceClonePro-Cleanup"
  ```

---

### Finding 2: Lack of Internal Process Exclusivity Lock (HIGH)
- **File / Component:** [`main.py`](file:///C:/Users/awint/VoiceCloneProOfficial/main.py)
- **Observed Behavior:**
  - `main.py` contains no PID file creation (`.pid`), no operating-system file lock (`msvcrt.locking` on Windows), no named mutex, and no local socket bind.
  - If an operator accidentally executes `python main.py --mode all` in two separate command prompts, both will launch concurrently.
- **Impact:**
  - For the Bot: Telegram API server will enforce single-poller semantics by returning HTTP 409 Conflict.
  - For the Worker: Atomic SQL claims (`UPDATE jobs SET status = 'RUNNING' WHERE status = 'QUEUED'`) prevent duplicate job execution, but competing workers would fight for Applio GPU/RAM and CPU audio encoding.
- **Verification Status:** **VERIFIED**
- **Severity:** **HIGH**
- **Safe Recommendation:**
  Operator must ensure strictly ONE console or service manager instance of `main.py` is launched. A lightweight PID file or mutex check should be slated for post-v1.0.0 maintenance.

---

### Finding 3: Startup Database Writes & Schema Mutation (LOW / SAFE)
- **File / Component:** [`app/db/session.py#L40-L49`](file:///C:/Users/awint/VoiceCloneProOfficial/app/db/session.py#L40-L49), [`app/services/recovery.py#L23-L64`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/recovery.py#L23-L64)
- **Observed Behavior:**
  - `init_db()` calls `Base.metadata.create_all`. Because all 15 tables already exist in `data/voice_clone_official.db`, SQLite executes `CREATE TABLE IF NOT EXISTS`, altering zero schema structures and inserting zero rows.
  - There are NO seed scripts or dummy data insertions.
  - `recover_stale_running_jobs()` scans for `Job.status == 'RUNNING'`. Read-only query confirms 0 jobs in `RUNNING` status (all 3 existing jobs are `SUCCESS`).
- **Impact:**
  - Zero database records are altered, deleted, or inserted during the startup sequence.
- **Verification Status:** **VERIFIED**
- **Severity:** **LOW (SAFE)**

---

### Finding 4: Existing Queue & Pending Job Processing Risk (LOW / SAFE)
- **File / Component:** [`app/worker.py#L180-L192`](file:///C:/Users/awint/VoiceCloneProOfficial/app/worker.py#L180-L192), [`app/services/queue.py#L106-L126`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/queue.py#L106-L126)
- **Observed Behavior:**
  - Worker loop calls `claim_next_queued_job()`, filtering strictly on `Job.status == 'QUEUED'`.
  - Read-only inspection of the production database confirms:
    - Total jobs: 3
    - Statuses: 3 `SUCCESS` (IDs `6b98be48...`, `cdbbd7c9...`, `cbfaf31d...`)
    - Queued jobs: 0
    - Running jobs: 0
- **Impact:**
  - The worker will find 0 candidate jobs upon boot. No unintended audio jobs or legacy retries will execute.
- **Verification Status:** **VERIFIED**
- **Severity:** **LOW (SAFE)**

---

### Finding 5: Shutdown and Signal Handling Completeness (MEDIUM)
- **File / Component:** [`main.py#L74-L84`](file:///C:/Users/awint/VoiceCloneProOfficial/main.py#L74-L84)
- **Observed Behavior:**
  - When `KeyboardInterrupt` (Ctrl+C) is caught, `main.py` executes:
    ```python
    finally:
        for t in tasks:
            t.cancel()
        if bot:
            await bot.session.close()
    ```
  - It does NOT explicitly await `asyncio.gather(*tasks, return_exceptions=True)`.
  - It does NOT explicitly invoke `await engine.dispose()`.
  - Subprocess cancellation: [`app/services/audio.py#L171`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/audio.py#L171) catches `TimeoutError` to kill `proc`, but if cancelled via task cancellation while Applio inference is running, the subprocess may linger until Python process termination.
- **Impact:**
  - Python process termination on Windows releases file locks and child handles. SQLite WAL shared memory files (`.db-shm`, `.db-wal`) will remain on disk until next opened or checkpointed.
- **Verification Status:** **VERIFIED**
- **Severity:** **MEDIUM**
- **Safe Recommendation:**
  When stopping the bot interactively, allow 3-5 seconds after pressing `Ctrl+C` for the process to exit completely.

---

### Finding 6: Reconciliation with Phase D21 Pre-Launch Report
- **File / Component:** [`phase_d21_prelaunch_safety_report.md`](file:///C:/Users/awint/VoiceCloneProOfficial/phase_d21_prelaunch_safety_report.md)
- **Comparison & Corrections:**
  1. **CHK-11 (Process Exclusivity):** D21 reported `PASS (0 active Python processes)`. D22 confirms that while 0 Python processes were running at that single moment, Windows Task Scheduler tasks `VoiceClonePro-Bot` and `VoiceClonePro-NativeWorker` are actively looping and spawning Python every 180 seconds. D21's claim of process exclusivity is **overruled and blocked**.
  2. **CHK-03 (Token Alignment):** D21 confirmed token alignment in memory. D22 confirms that this alignment makes the active legacy scheduled task conflict dangerous.
  3. **CHK-05 to CHK-10 (Database Integrity & Backup):** Verified intact and consistent across both audits.
  4. **CHK-12 to CHK-15 (Dependencies, Models, Tests):** Verified intact and consistent across both audits.

---

## 4. Current Database & Queue State (Read-Only Evidence)

Database connection string: `file:C:\Users\awint\VoiceCloneProOfficial\data\voice_clone_official.db?mode=ro`

| Table Name | Row Count | Status / Notes |
| :--- | :---: | :--- |
| `users` | 78 | 1 Admin (`1957002115`), 77 imported legacy users. |
| `minute_balances` | 78 | All 77 non-admin users have `0` seconds across all buckets. |
| `minute_ledger_events` | 0 | Clean ledger. Zero initial transactions. |
| `jobs` | 3 | All 3 jobs are `SUCCESS`. 0 `QUEUED`, 0 `RUNNING`. |
| `audit_logs` | 4 | Historical administrative audit entries. |
| `voice_models` | 2 | `တွတ်ပီ` (`ACTIVE`), `အကိုလေး` (`READY`). File paths match disk. |
| `pricing_plans` | 0 | Default system tiers used. |
| `payment_requests` | 0 | Clean. |
| `broadcast_campaigns` | 0 | Clean. |
| `broadcast_deliveries` | 0 | Clean. |
| `adjustment_idempotency_records` | 0 | Clean. |
| `welcome_bonus_awards` | 0 | Clean. |
| `referrals` | 0 | Clean. |
| `referral_rewards` | 0 | Clean. |
| `bot_settings` | 0 | Clean. |

---

## 5. Backup, Restore, and Rollback Procedures

### 5.1 SQLite WAL Backup & Checkpoint
1. **SQLite WAL Architecture:** The database operates in WAL mode with `voice_clone_official.db`, `voice_clone_official.db-wal`, and `voice_clone_official.db-shm`.
2. **Safe Backup Procedure:** Never copy only the `.db` file while the service is writing. Always perform backups when stopped, or via SQLite online backup / `VACUUM INTO`:
   ```powershell
   # When service is stopped:
   Copy-Item data\voice_clone_official.db data\backups\voice_clone_official_prelaunch_manual.db
   ```
3. **Current Available Backup:**
   `data/backups/voice_clone_official_pre_d20_20261013_031407.db` (180,224 bytes) contains the pre-migration baseline (1 admin user).

### 5.2 Rollback Risks & Post-Launch Data Protection
- **Pre-Launch Rollback:** Restoring `voice_clone_official_pre_d20_...` right now reverts the database to 1 user and restores previous environment.
- **Post-Launch Rollback Caution:** Once the bot is live, users who register, verify channels, or purchase minutes will write new rows to `users`, `minute_balances`, and `minute_ledger_events`. Restoring an old backup file after launch will **permanently destroy** post-launch user data.
- **Token Rollback:** If the operator reverts `.env` to the pre-D20 token (`8907098555:...`), all 78 registered users in the active bot chat will experience broken communications.

---

## 6. Pre-Launch Gate Checklist

| Checklist Item | Current State | Required State | Gate Status |
| :--- | :---: | :---: | :---: |
| **Git Release Head & Tag** | Commit `5231f89f...` matches `v1.0.0-production` | Exact match | **PASS** |
| **Tracked File Cleanliness** | 0 uncommitted changes to tracked files | 0 uncommitted changes | **PASS** |
| **Automated Tests** | 132/132 passed (23.82s) | 100% pass | **PASS** |
| **Database Integrity** | `PRAGMA integrity_check` = `ok` | `ok` | **PASS** |
| **Foreign Key Check** | 0 violations | 0 violations | **PASS** |
| **Queue State** | 0 `QUEUED`, 0 `RUNNING` | 0 active jobs | **PASS** |
| **Voice Model Assets** | `တွတ်ပီ` & `အကိုလေး` present on disk | Present & accessible | **PASS** |
| **Applio & FFmpeg Paths** | Verified on disk | Verified on disk | **PASS** |
| **Token Match** | Aligned with legacy identity (`8631167018`) | Aligned | **PASS** |
| **Legacy Scheduled Tasks** | `VoiceClonePro-Bot` & `Worker` running every 180s | **STOPPED & DISABLED** | **BLOCKED** |
| **Live Telegram API Test** | Not tested (prohibited in read-only audit) | Tested upon operator launch | **PENDING LAUNCH** |

---

## 7. Action Items for Operator Prior to Launch

To resolve the remaining blocker and launch the service:

1. **Stop & Disable Legacy Windows Scheduled Tasks:**
   Open an Administrator PowerShell prompt and execute:
   ```powershell
   Stop-ScheduledTask -TaskName "VoiceClonePro-Bot", "VoiceClonePro-NativeWorker" -ErrorAction SilentlyContinue
   Disable-ScheduledTask -TaskName "VoiceClonePro-Bot", "VoiceClonePro-NativeWorker", "VoiceClonePro-Cleanup"
   ```
   *Verify that no PowerShell supervisor processes remain running `run_bot.ps1` or `run_worker.ps1`.*

2. **Launch VoiceClonePro Official in Unified Mode:**
   ```powershell
   cd C:\Users\awint\VoiceCloneProOfficial
   & "C:\Users\awint\AppData\Local\Programs\Python\Python310\python.exe" main.py --mode all
   ```

3. **Verify Boot Output:**
   - Database verified
   - Bot commands menu registered (`/start`)
   - Worker loop started
   - Polling started without HTTP 409 Conflict

4. **Perform Live Smoke Test:**
   - Admin account (`@VVin_ts`, `1957002115`) sends `/start` to bot.
   - Verify Myanmar start menu and inline Admin dashboard.
