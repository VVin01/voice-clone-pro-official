# Phase D14-A-R — Independent Safety Review Report

**Workspace:** `C:\Users\awint\VoiceCloneProOfficial`  
**Legacy Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` (Strictly untouched & read-only)  
**Role:** Independent Senior Python/SQLAlchemy Engineer & Production Release Auditor  
**Date:** 2026-10-12  
**Final Verdict:** **BLOCKED — FIX REQUIRED**

---

## 1. Executive Summary

This independent safety review examined the completed **Phase D14-A** implementation, tests, and evidence report against the core product architecture (minutes-only model, `FREE`/`PURCHASED` account types, strict admin isolation).

While the user management pagination, detail inspection, HTML escaping, and ledger debit-bounds validation are cleanly implemented, two safety concerns were identified:
1. **[BLOCKER] Uncoordinated Live Queue Recovery UI Trigger**: Exposing `recover_stale_running_jobs` as an interactive button in the Telegram Admin Dashboard while background workers may be actively running lacks a worker heartbeat or process liveness check. A recovery sweep triggered during execution of an active job running $> 600\text{s}$ will prematurely mark the job `FAILED`, delete its temporary working directory from underneath the worker, and conflict with final audio dispatch and ledger billing.
2. **[HIGH] Absence of Durable Database Unique Constraint for Idempotency**: `MinuteLedgerEvent.reference_id` lacks a database-level `UniqueConstraint`. Idempotency deduplication relies exclusively on an in-memory aiogram FSM state check and a software `SELECT` query prior to insert. Under concurrent multi-request scenarios on SQLite, two racing requests with the same token can both succeed.

---

## 2. Check-by-Check Audit Matrix

| # | Review Check Area | Status | Key Finding | Primary Reference |
| :---: | :--- | :---: | :--- | :--- |
| **1** | **Ledger Correctness** | **PASS** | Purchased, welcome, referral, and consumed minutes are strictly tracked in seconds with non-negative constraints. Debit operations only decrement `purchased_seconds` and cannot consume protected welcome/referral balances. Ledger events and audit logs are atomic with balance changes. | [`app/services/ledger.py#L487-L580`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/ledger.py#L487-L580) |
| **2** | **Idempotency & Concurrency** | **FAIL** | Token deduplication in aiogram memory is effective for sequential double-taps. However, `MinuteLedgerEvent.reference_id` has no `UniqueConstraint` in the schema. In SQLite, `with_for_update()` is ignored; concurrent transactions can execute parallel reads and apply duplicate adjustments. | [`app/models/schema.py#L147-L175`](file:///C:/Users/awint/VoiceCloneProOfficial/app/models/schema.py#L147-L175)<br>[`app/services/ledger.py#L503-L519`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/ledger.py#L503-L519) |
| **3** | **Authorization & Callbacks** | **PASS** | Every adjustment and queue callback strictly enforces `is_authorized_admin`. Malformed, stale, expired, and replayed tokens fail safely with alerts. Success UI edits are dispatched only after successful transaction commit. | [`app/handlers/admin.py#L728-L1015`](file:///C:/Users/awint/VoiceCloneProOfficial/app/handlers/admin.py#L728-L1015) |
| **4** | **Queue Stale Recovery Safety** | **BLOCKED** | Real recovery criteria requires `Job.status == 'RUNNING'` and `started_at <= utc_now() - 600s`. The recovery routine is not safe to trigger arbitrarily from a Telegram button while a worker process is running. No worker heartbeat/PID check exists. Wiping temporary files while a worker is running will induce I/O collisions on Windows. | [`app/services/recovery.py#L43-L135`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/recovery.py#L43-L135)<br>[`app/handlers/admin.py#L565-L592`](file:///C:/Users/awint/VoiceCloneProOfficial/app/handlers/admin.py#L565-L592) |
| **5** | **Tests & Localization** | **PASS** | 123/123 tests passing. 100% Burmese/English key parity. Terminology completely cleansed of "tier" / "pricing plans" in favor of "Minute Packages" and `FREE`/`PURCHASED` account status. Native Bot Commands Menu contains strictly `/start` in `BotCommandScopeDefault`. | [`tests/unit/test_admin_user_management.py`](file:///C:/Users/awint/VoiceCloneProOfficial/tests/unit/test_admin_user_management.py)<br>[`tests/unit/test_bot_commands.py`](file:///C:/Users/awint/VoiceCloneProOfficial/tests/unit/test_bot_commands.py) |

---

## 3. Deep-Dive Findings & Technical Analysis

### 3.1. Ledger Correctness (PASS)
- **Balance Model**: [`MinuteBalance`](file:///C:/Users/awint/VoiceCloneProOfficial/app/models/schema.py#L87-L133) maintains discrete non-negative BigInteger fields for `welcome_seconds`, `referral_seconds`, `purchased_seconds`, and `consumed_seconds`.
- **Protected Welcome/Referral Protection**: In [`admin_adjust_balance`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/ledger.py#L528-L549), `direction == DEBIT` with `source == PURCHASE` explicitly enforces:
  ```python
  if balance.purchased_seconds < seconds:
      raise ValueError(f"Insufficient purchased balance ({balance.purchased_seconds}s) to debit {seconds}s")
  balance.purchased_seconds -= seconds
  ```
  Protected welcome and referral minutes cannot be depleted via admin adjustment.
- **Account Category Promotion & Demotion**:
  - Crediting purchased seconds auto-promotes `account_category` to `PURCHASED`.
  - Debiting purchased seconds down to 0 sets `account_category` to `FREE`.
- **Atomicity**: `MinuteLedgerEvent(source=ADJUST)` and `AuditLog` are flushed and committed in the exact same transaction as the balance mutation.

---

### 3.2. Idempotency & Concurrency (FAIL)
- **In-Memory FSM Protection**: In [`handle_admin_adjust_confirm`](file:///C:/Users/awint/VoiceCloneProOfficial/app/handlers/admin.py#L945-L950), the FSM state is cleared before database execution (`await state.clear()`). Any subsequent callback tap in the same session is rejected as expired/invalid.
- **Missing Database Constraint**: In [`app/models/schema.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/models/schema.py#L147-L175), `MinuteLedgerEvent` defines:
  ```python
  reference_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
  ```
  There is **no** `UniqueConstraint` on `reference_id` or `(user_id, reference_id)`.
- **Database Engine Concurrency**: The application runs SQLite (`sqlite+aiosqlite:///./data/voice_clone_official.db`). Under SQLite:
  - `select(...).with_for_update()` is ignored by the database engine (SQLite does not support row-level locks).
  - Transactions begin deferred (`BEGIN DEFERRED`).
  - Two concurrent requests can execute `SELECT FROM minute_ledger_events WHERE reference_id = :key` at the same time, both observe no existing record, and both insert duplicate credit rows without database error.
- **Conclusion**: Idempotency is enforced only in application memory, not via durable database constraints.

---

### 3.3. Authorization & Callbacks (PASS with observation)
- **Authorization Coverage**: Every callback and message handler validates `is_authorized_admin(event)` against `settings.admin_ids_raw`.
- **Token Verification**: In [`handle_admin_adjust_confirm`](file:///C:/Users/awint/VoiceCloneProOfficial/app/handlers/admin.py#L926), token matching validates:
  ```python
  if not expected_token or expected_token != token or not target_user_id or not minutes or not clean_reason:
      await callback.answer("⚠️ Adjustment confirmation is invalid or has expired.", show_alert=True)
      return
  ```
- **Observation on Transaction Rollback**: In `handle_admin_adjust_confirm`, if `session is not None` and an exception is raised, `await session.rollback()` is not explicitly called inside the `except Exception as e:` block. While the `else` block (`async with AsyncSessionLocal() as s:`) rolls back on context exit, callers passing an external session could be left with an un-rolled-back session.

---

### 3.4. Queue Stale Job Recovery Safety (BLOCKED)
- **Original Architecture**: [`recover_stale_running_jobs`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/recovery.py#L23) was designed as a **startup recovery sweep** executed in [`worker/worker.py#L198`](file:///C:/Users/awint/VoiceCloneProOfficial/app/worker.py#L198) before worker polling begins.
- **Live UI Exposure**: Exposing this as an ad-hoc button in the Telegram Admin Dashboard (`admin:queue_recover_confirm`) introduces unsafe race conditions when a background worker is actively running:
  1. **Absence of Worker Coordination**: There is no worker heartbeat table, OS PID check, or advisory lock indicating whether a background worker is actively processing jobs.
  2. **Active Job File Collisions**: If a job runs longer than `stale_job_timeout_seconds` (600s), `recover_stale_running_jobs` unconditionally wipes the job's temporary working folder:
     ```python
     temp_dir = settings.storage_root / "temp" / str(job.id)
     if temp_dir.exists():
         shutil.rmtree(temp_dir, ignore_errors=True)
     ```
     On Windows, deleting open files while a worker process is actively generating or converting audio causes `PermissionError` or subsequent `FileNotFoundError`.
  3. **State Inconsistency**: If recovery marks a job `FAILED` and refunds balance while the worker finishes inference, the worker will subsequently mark the job `SUCCESS` and dispatch audio, resulting in free uncharged generation and conflicting status messages to the user.
  4. **UI Text Discrepancy**: The confirmation prompt states *"longer than 5 minutes"*, whereas the backend actually enforces 600 seconds (10 minutes).
- **Mandatory Verdict**: Per instructions, because safety cannot be demonstrated while a worker is running, this action is classified as **BLOCKED**.

---

### 3.5. Tests & Localization (PASS)
- **Test Coverage**: All 123 tests pass across unit, worker, crash recovery, and security modules.
- **Terminology Parity**: Replaced "tier" with "account type / status" and "pricing plans" with "Minute Packages" across Burmese and English locale files.
- **Telegram Native Commands Menu**: Verified that `BotCommandScopeDefault` registers **only `/start`**.

---

## 4. Severity-Ranked Defects & Remediations

### Defect 1: Uncoordinated Live Queue Recovery UI Action
- **Severity**: **BLOCKER**
- **Impact**: Premature failure, temporary directory wiping, and race conditions on active jobs exceeding 10 minutes while a worker is running.
- **Minimum Safe Remediation**:
  1. Remove or disable the live manual recovery button (`admin:queue_recover_prompt` / `admin:queue_recover_confirm`) from the Admin Dashboard UI.
  2. Retain `recover_stale_running_jobs` strictly as a startup-time recovery routine when no worker is running.
  3. If manual recovery from UI is required in future releases, require a verified worker heartbeat check confirming that no worker is running before allowing execution.
  4. Correct the UI message text from "5 minutes" to match `stale_job_timeout_seconds` (10 minutes / 600s).

### Defect 2: Lack of Durable Database Constraint on Idempotency Reference ID
- **Severity**: **HIGH**
- **Impact**: In-memory FSM deduplication does not protect against concurrent requests at the database engine level.
- **Minimum Safe Remediation**:
  1. Add a unique constraint to `MinuteLedgerEvent` in [`app/models/schema.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/models/schema.py):
     ```python
     UniqueConstraint("reference_id", name="uq_ledger_event_reference_id")
     ```
     (or `UniqueConstraint("user_id", "reference_id", name="uq_ledger_user_ref")` for non-null reference IDs).
  2. Catch `IntegrityError` in `admin_adjust_balance` to gracefully return the existing balance on database-level replay.

### Defect 3: Missing Explicit Rollback on Exception in Handler
- **Severity**: **LOW**
- **Impact**: In `handle_admin_adjust_confirm`, an exception when `session is not None` does not call `session.rollback()`.
- **Minimum Safe Remediation**:
  ```python
  except Exception as e:
      if session is not None:
          await session.rollback()
      logger.error(...)
  ```

---

## 5. Reviewer Verdict

**Verdict:** **BLOCKED — FIX REQUIRED**

### Conditions for Unblocking:
1. Disable or gate the interactive queue stale recovery button in the Admin Dashboard until worker liveness coordination is implemented.
2. Add durable unique constraints for ledger reference ID idempotency.
3. Add missing concurrent execution unit tests verifying database-level duplicate suppression.
