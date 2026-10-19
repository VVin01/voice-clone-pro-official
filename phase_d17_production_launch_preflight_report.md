# Phase D17 — Production Launch Preflight Audit Report (READ-ONLY)

**Workspace:** `C:\Users\awint\VoiceCloneProOfficial`  
**Legacy Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` *(Strictly Read-Only — Untouched)*  
**Date:** 2026-10-13  
**Auditor / Release Inspector:** Senior Python, Infrastructure & Reliability Engineer  

---

## 1. Executive Summary & Launch Recommendation

### **EXPLICIT LAUNCH RECOMMENDATION: READY FOR OPERATOR APPROVAL**

Release `v1.0.0-production` has undergone a comprehensive, strictly read-only preflight audit on this Windows production machine:
1. **Commit & Tag Alignment**: HEAD and annotated tag `v1.0.0-production` resolve to the exact release commit `5231f89fe30d55cb109df51d69a6524fe1e8e379`.
2. **Environment & Dependencies**: Python 3.10.11 and all 9 pinned third-party packages in `requirements.txt` are an exact 1:1 match.
3. **Configuration Completeness**: All required `.env` variables (`TELEGRAM_BOT_TOKEN`, `ADMIN_IDS`, `REQUIRED_CHANNEL_ID`, `REQUIRED_GROUP_ID`, `DATABASE_URL`, `APPLIO_WORKDIR`) are present and populated without secrets exposed.
4. **Zero Startup Mutation Risk**: Statically verified that `Base.metadata.create_all` issues `CREATE TABLE IF NOT EXISTS` against all 15 already existing tables. Zero `RUNNING` or `QUEUED` jobs exist in the production database (all 3 jobs are `SUCCESS`), meaning startup recovery will perform 0 mutations or refunds on boot.
5. **Runtime Binaries & Models**: FFmpeg 9.0.1 and Applio Python (`C:\Applio\env\python.exe`) are verified callable. Both system voice models (`တွတ်ပီ` and `အစ်ကိုလေး`) have valid `.pth` and `.index` artifacts present.
6. **Full Test Isolation & Regression**: 132/132 unit, concurrency, crash recovery, and security tests passed in 18.28s under strict in-memory database isolation.
7. **Process Hygiene**: 0 Python bot or worker processes are running. The environment is clean and ready.

---

## 2. Preflight Audit Checklist (PASS / WARN / BLOCKED)

| Item | Scope / Check | Verdict | Evidence / Details |
| :--- | :--- | :---: | :--- |
| **1. Commit & Tag Target** | HEAD & `v1.0.0-production` target commit | **PASS** | Both resolve to `5231f89fe30d55cb109df51d69a6524fe1e8e379`. |
| **2. Python Runtime** | Host Python version matching 3.10.x | **PASS** | `Python 3.10.11 (AMD64)` active. |
| **3. Pinned Dependencies** | All 9 packages match `requirements.txt` exactly | **PASS** | `aiogram==3.31.0`, `sqlalchemy==2.0.52`, `aiosqlite==0.22.1`, `greenlet==3.5.5`, `pydantic==2.13.5`, `pydantic-settings==2.15.0`, `edge-tts==7.2.8`, `pytest==9.1.1`, `pytest-asyncio==1.4.0`. |
| **4. Configuration Keys** | Required variables in `.env` present & non-empty | **PASS** | Verified via AST/dotenv parse. Zero values printed. |
| **5. SQLite Startup Safety** | Schema & data mutation risks during boot | **PASS** | `CREATE TABLE IF NOT EXISTS` is a no-op (15 tables exist). Zero `RUNNING` jobs; recovery sweep performs 0 writes. |
| **6. Stored Audio Models** | Model `.pth` and `.index` files present | **PASS** | 2 system model pairs: `kartoon` (55.1MB / 42.5MB) and `ant` (55.1MB / 248.5MB). |
| **7. External Binaries** | FFmpeg and Applio Python callable | **PASS** | `C:\ffmpeg\bin\ffmpeg.EXE` (v9.0.1) and `C:\Applio\env\python.exe` confirmed. |
| **8. Admin & Ledger Rules** | Unlimited admin, bucket accounting, command scoping | **PASS** | Handlers check `settings.is_admin()`, unlimited audio bypass in ledger, native Commands Menu restricted to `/start`. |
| **9. Process Concurrency** | Single-instance guard / running processes | **WARN** | 0 Python processes running now. However, `main.py` lacks a local pidfile; operator must run only one instance to prevent Telegram HTTP 409 conflict. |
| **10. Full Regression Test** | Execution of complete test suite under isolation | **PASS** | **132 / 132 PASSED** in 18.28s. Zero production resources touched. |

---

## 3. Detailed Audit Findings

### 3.1. Release Commit & Tag Verification
- **Command:** `git rev-parse HEAD; git rev-parse "v1.0.0-production^{commit}"`
- **Output:**
  ```text
  HEAD:                     5231f89fe30d55cb109df51d69a6524fe1e8e379
  v1.0.0-production:       5231f89fe30d55cb109df51d69a6524fe1e8e379
  ```
- **Fidelity:** Exactly matches the 54-file commit created in Phase D16-F.

### 3.2. Pinned Package Versions vs. Requirements
- All 9 packages installed in the active environment were checked via `importlib.metadata`:
  - `aiogram`: `3.31.0` (Match)
  - `sqlalchemy`: `2.0.52` (Match)
  - `aiosqlite`: `0.22.1` (Match)
  - `greenlet`: `3.5.5` (Match)
  - `pydantic`: `2.13.5` (Match)
  - `pydantic-settings`: `2.15.0` (Match)
  - `edge-tts`: `7.2.8` (Match)
  - `pytest`: `9.1.1` (Match)
  - `pytest-asyncio`: `1.4.0` (Match)
- **Result:** 100% dependency compatibility verified.

### 3.3. Environment Configuration Inspection (Values Redacted)
- **Required Keys:**
  - `TELEGRAM_BOT_TOKEN`: PRESENT & NON-EMPTY
  - `ADMIN_IDS`: PRESENT & NON-EMPTY
  - `REQUIRED_CHANNEL_ID`: PRESENT & NON-EMPTY
  - `REQUIRED_GROUP_ID`: PRESENT & NON-EMPTY
  - `DATABASE_URL`: PRESENT & NON-EMPTY (`sqlite+aiosqlite:///./data/voice_clone_official.db`)
  - `APPLIO_WORKDIR`: PRESENT & NON-EMPTY (`C:\Applio`)
- **Optional / Defaults Configured:**
  - `CHANNEL_INVITE_LINK`: CONFIGURED
  - `GROUP_INVITE_LINK`: CONFIGURED
  - `APPLIO_PYTHON`: CONFIGURED (`C:\Applio\env\python.exe`)
  - `APPLIO_RUNNER`: CONFIGURED (`worker/applio_runner.py`)
  - `TUTORIAL_VIDEO_LINK`: CONFIGURED
  - `STALE_JOB_TIMEOUT_SECONDS`: DEFAULTED (600s)

### 3.4. Production Database & Schema Mutation Analysis
- **Read-Only SQLite URI Query (`file:data/voice_clone_official.db?mode=ro`):**
  - Existing Tables: 15 tables present (`users`, `minute_balances`, `minute_ledger_events`, `voice_models`, `user_voice_access`, `jobs`, `payment_intents`, `referrals`, `audit_logs`, `broadcast_campaigns`, `broadcast_deliveries`, `daily_stats`, `adjustment_idempotency_records`, etc.).
  - Existing Jobs: Exactly 3 jobs, all with status `SUCCESS`. Zero jobs in `RUNNING` or `QUEUED` state.
- **Startup Code Mutation Assessment:**
  - `init_db()` executes `Base.metadata.create_all`. Because all 15 tables already exist, this is purely an `IF NOT EXISTS` check and performs **zero schema modifications**.
  - `recover_stale_running_jobs()` scans for `RUNNING` jobs older than 600 seconds. Because 0 jobs are running, the recovery query returns `[]` and performs **zero database writes or refunds**.
  - **Verdict:** Launching the service will not unexpectedly mutate existing schema or historical accounting data.

### 3.5. Applio & Audio Pipeline Prerequisites
- **FFmpeg Executable:** `C:\ffmpeg\bin\ffmpeg.EXE` (Version: `9.0.1-essentials_build-www.gyan.dev`). Callable on PATH.
- **Applio Python Runtime:** `C:\Applio\env\python.exe` exists and is accessible.
- **Applio Runner Script:** `worker/applio_runner.py` exists (104 lines).
- **System Models Present:**
  - `storage\models\system\တွတ်ပီ\kartoon_150e_1950s.pth` (55,174,290 bytes) + `kartoon.index` (42,544,179 bytes).
  - `storage\models\system\အစ်ကိုလေး\ant_150e_11700s.pth` (55,172,901 bytes) + `ant.index` (248,516,099 bytes).

### 3.6. Worker & Error Handling Architecture
- **Queue Polling & Claiming:** `claim_next_queued_job` executes atomic SQL updates (`UPDATE jobs SET status = 'RUNNING' WHERE id = :id AND status = 'QUEUED'`) and checks `rowcount == 1`.
- **Failure Resilience:** Any error during Edge TTS, Audio Normalization, Applio inference, or Telegram delivery catches `Exception`, marks job `FAILED`, sanitizes error text for user UI, and triggers `refund_job_seconds` (unless user is an admin).
- **Graceful Shutdown:** `main.py` catches `KeyboardInterrupt` and `asyncio.CancelledError`, cancels running background tasks, and closes the `aiogram.Bot` aiohttp session cleanly.

---

## 4. Isolated Test Suite Verification

- **Command:** `python -m pytest -v --tb=short`
- **Isolation:** Tests execute strictly against `sqlite+aiosqlite:///:memory:` and temporary directory SQLite databases. Production database `data/voice_clone_official.db` is completely untouched.
- **Test Modules:** 19 modules
- **Total Tests:** 132 tests
- **Passed:** **132**
- **Failed:** **0**
- **Duration:** 18.28 seconds
- **Pass Rate:** **100%**

---

## 5. Unresolved Risks & Operational Constraints

1. **Telegram Single-Instance Requirement (WARN)**:
   - Aiogram long-polling does not maintain an OS-level file lock. If an operator launches a second instance of `python main.py --mode bot` or `--mode all`, Telegram Bot API will return HTTP 409 Conflict (`TelegramConflictError: Conflict: terminated by other getUpdates request`).
   - *Mitigation*: Ensure only one process is launched. Always check `Get-Process python` before starting.
2. **First-Inference GPU Latency**:
   - The first Applio voice conversion request will load PyTorch models and CUDA kernels into VRAM, which typically takes 3–8 seconds longer than subsequent conversions. This is expected behavior.
3. **Receipt Verification**:
   - The administrative payment flow requires manual operator visual verification of KBZPay / WavePay screenshots before clicking the inline `Approve` button.

---

## 6. Launch Runbook (Awaiting Operator Execution)

When the operator decides to start the production service:

```powershell
# 1. Verify no existing Python process is running
Get-Process python -ErrorAction SilentlyContinue

# 2. Launch combined Telegram Bot and Background Audio Worker
python main.py --mode all
```

To stop gracefully: Press `Ctrl+C` once and observe `Service shut down cleanly.` in the console.
