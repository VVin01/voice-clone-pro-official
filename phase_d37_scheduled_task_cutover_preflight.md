# D37 — Scheduled Task Cutover Preflight & Repair Plan

**Evaluation Date:** October 14, 2026  
**Auditor:** Senior Release Engineer & Systems Architect  
**Workspace:** `C:\Users\awint\VoiceCloneProOfficial`  
**Legacy Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` *(Strictly Read-Only — Verified Untouched)*  
**Current Baseline HEAD:** `e05a2aa9c087663ee41f9f1ff2a4655b0da435d4` on branch `master`  
**Operating Policy:** READ-ONLY AUDIT & PLANNING ONLY (No tasks modified, no services launched, no files committed)

---

## 1. Executive Summary

During Phase D36, a P0 operational blocker was identified: the host Windows Scheduled Tasks (`VoiceClonePro-Bot` and `VoiceClonePro-NativeWorker`) point to scripts in the **legacy workspace** (`C:\Users\awint\VOICE_CLONE_PRO_Production_Final`), which rely on legacy PostgreSQL/Docker Compose and obsolete `bot.py` entry points.

This document establishes the verified technical blueprint to cut over Windows Scheduled Tasks to the official `VoiceCloneProOfficial` repository cleanly and safely.

* **Current Scheduled Task State:** All operational tasks (`VoiceClonePro-Bot`, `VoiceClonePro-NativeWorker`) are verified to be in the **Disabled** state. They cannot trigger unexpected background execution.
* **Cleanup Task State:** `VoiceClonePro-Cleanup` is currently **Ready** (enabled daily at 4:15 AM), targeting the legacy workspace. Because `VoiceCloneProOfficial` performs inline temporary file cleanup per job and on boot recovery, the legacy cleanup task is incompatible with the new SQLite schema and must be disabled or replaced.
* **Proposed Target Startup:** `main.py` in `C:\Users\awint\VoiceCloneProOfficial` supports standalone (`--mode bot`, `--mode worker`) or unified (`--mode all`) modes powered by the project's Python 3.10 virtual environment (`.venv\Scripts\python.exe`).
* **Execution Status in D37:** **Zero tasks or services were altered in this phase.** All findings and proposed cutover steps are recorded for a separately authorized repair phase.

---

## 2. Evidence of Current Scheduled Task Targets & States

Live XML queries against Windows Task Scheduler (`schtasks /query /xml`) captured the exact configuration of all three tasks:

### Task 1: `\VoiceClonePro-Bot`
* **Status:** `Disabled`
* **Principal:** `NT AUTHORITY\SYSTEM` (SID: `S-1-5-18`), `RunLevel: HighestAvailable`
* **Trigger:** Boot Trigger with 1-minute startup delay (`<BootTrigger><Delay>PT1M</Delay></BootTrigger>`)
* **Restart Policy:** 3 restarts at 1-minute intervals (`<RestartOnFailure><Count>3</Count><Interval>PT1M</Interval></RestartOnFailure>`)
* **Execution Limit:** Indefinite (`PT0S`)
* **Instance Policy:** `IgnoreNew`
* **Target Command:** `powershell.exe`
* **Target Arguments:** `-NoProfile -ExecutionPolicy Bypass -File "C:\Users\awint\VOICE_CLONE_PRO_Production_Final\scripts\run_bot.ps1"`
* **Working Directory:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` [LEGACY MISALIGNMENT]

### Task 2: `\VoiceClonePro-NativeWorker`
* **Status:** `Disabled`
* **Principal:** `NT AUTHORITY\SYSTEM` (SID: `S-1-5-18`), `RunLevel: HighestAvailable`
* **Trigger:** Boot Trigger with 1-minute startup delay (`<BootTrigger><Delay>PT1M</Delay></BootTrigger>`)
* **Restart Policy:** 3 restarts at 1-minute intervals (`<RestartOnFailure><Count>3</Count><Interval>PT1M</Interval></RestartOnFailure>`)
* **Execution Limit:** Indefinite (`PT0S`)
* **Instance Policy:** `IgnoreNew`
* **Target Command:** `powershell.exe`
* **Target Arguments:** `-NoProfile -ExecutionPolicy Bypass -File "C:\Users\awint\VOICE_CLONE_PRO_Production_Final\scripts\run_worker.ps1"`
* **Working Directory:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` [LEGACY MISALIGNMENT]

### Task 3: `\VoiceClonePro-Cleanup`
* **Status:** `Ready` (Scheduled daily at `04:15:00+06:30`)
* **Principal:** User `awint` (SID: `S-1-5-21-2478637745-1406525212-2116227515-1001`), `InteractiveToken`
* **Trigger:** Daily Calendar Trigger (`<DaysInterval>1</DaysInterval>`)
* **Last Result:** Exit code `1`
* **Target Command:** `powershell.exe`
* **Target Arguments:** `-NoProfile -ExecutionPolicy Bypass -File "C:\Users\awint\VOICE_CLONE_PRO_Production_Final\scripts\run_cleanup.ps1"`
* **Working Directory:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` [LEGACY MISALIGNMENT]

---

## 3. Verified Startup Interface from Source Code

Direct inspection of [`main.py`](file:///C:/Users/awint/VoiceCloneProOfficial/main.py#L86-L103) and [`README.md`](file:///C:/Users/awint/VoiceCloneProOfficial/README.md#L77-L102) in `VoiceCloneProOfficial` confirms the application's supported CLI contract:

```python
def main() -> None:
    parser = argparse.ArgumentParser(description="Voice Clone Pro Standalone Telegram Bot & Worker")
    parser.add_argument(
        "--mode",
        choices=["all", "bot", "worker"],
        default="all",
        help="Service execution mode: 'all' (bot + worker), 'bot', or 'worker'",
    )
    args = parser.parse_args()
```

### Verified Runtime Paths & Prerequisites
* **Virtual Environment Python:** `C:\Users\awint\VoiceCloneProOfficial\.venv\Scripts\python.exe` (Verified present: Python 3.10.11)
* **Application Root & Working Directory:** `C:\Users\awint\VoiceCloneProOfficial`
* **Configuration:** Loaded automatically from `C:\Users\awint\VoiceCloneProOfficial\.env` by `app/config/settings.py`.
* **ACL Verification:** Direct inspection with `icacls` confirms `NT AUTHORITY\SYSTEM` has Full Control (`(I)(OI)(CI)(F)`) over `C:\Users\awint\VoiceCloneProOfficial` and `C:\Applio`.

---

## 4. Task Compatibility & Architectural Comparison

| Dimension | Legacy Scheduled Task Implementation | Official Repository (`VoiceCloneProOfficial`) Implementation |
|---|---|---|
| **Database Architecture** | Docker Compose PostgreSQL (`scripts\boot_postgres.ps1`, port 5432) | Embedded asynchronous SQLite with WAL mode (`sqlite+aiosqlite:///./data/voice_clone_official.db`) |
| **Startup Scripts** | External `.ps1` wrappers (`run_bot.ps1`, `run_worker.ps1`, `run_cleanup.ps1`) | Unified Python CLI entry point (`main.py --mode [bot\|worker\|all]`) |
| **Crash Recovery** | Supervisor loops in PowerShell | In-process queue sweep at startup (`recover_stale_running_jobs` in `app/services/recovery.py`) |
| **Temporary File Retention** | Batch daily script (`run_cleanup.ps1` at 04:15 AM) | Real-time inline cleanup per synthesis job (`app/worker.py` line 180) |
| **Process Concurrency Risk** | Separate OS processes contending on PostgreSQL connection pool | If running two OS processes on SQLite, potential WAL write lock contention. Running `--mode all` in a single OS process provides 100% thread/coroutine coordination with zero SQLite lock risk. |

---

## 5. Per-Task Proposed Repair Plan

### A. Task `\VoiceClonePro-Bot`

* **Proposed State:** Retarget to official workspace (remain `Disabled` until live smoke testing).
* **Proposed Action Executable:** `C:\Users\awint\VoiceCloneProOfficial\.venv\Scripts\python.exe`
* **Proposed Arguments:** `main.py --mode bot`
* **Proposed Working Directory:** `C:\Users\awint\VoiceCloneProOfficial`
* **Principal:** `NT AUTHORITY\SYSTEM` (or `awint`)
* **Supervised Logging Enhancement:** To capture standard output and error without relying on an interactive console, the task action can be wrapped via PowerShell with non-blocking file appending:
  * Command: `powershell.exe`
  * Arguments: `-NoProfile -ExecutionPolicy Bypass -Command "& 'C:\Users\awint\VoiceCloneProOfficial\.venv\Scripts\python.exe' main.py --mode bot *>> 'C:\Users\awint\VoiceCloneProOfficial\logs\bot.log'"`
  * Working Directory: `C:\Users\awint\VoiceCloneProOfficial`

---

### B. Task `\VoiceClonePro-NativeWorker`

* **Proposed State:** Retarget to official workspace (remain `Disabled` until live smoke testing).
* **Proposed Action Executable:** `C:\Users\awint\VoiceCloneProOfficial\.venv\Scripts\python.exe`
* **Proposed Arguments:** `main.py --mode worker`
* **Proposed Working Directory:** `C:\Users\awint\VoiceCloneProOfficial`
* **Principal:** `NT AUTHORITY\SYSTEM`
* **Supervised Logging Enhancement:**
  * Command: `powershell.exe`
  * Arguments: `-NoProfile -ExecutionPolicy Bypass -Command "& 'C:\Users\awint\VoiceCloneProOfficial\.venv\Scripts\python.exe' main.py --mode worker *>> 'C:\Users\awint\VoiceCloneProOfficial\logs\worker.log'"`
  * Working Directory: `C:\Users\awint\VoiceCloneProOfficial`

---

### C. Recommended Alternative: Unified Service Deployment (`--mode all`)

Because SQLite WAL mode operates with highest throughput and zero lock friction when writer connections are managed within a single asyncio process, the recommended production architecture is to run the unified mode:
* **Single Task:** `VoiceClonePro-Service` (or configure `VoiceClonePro-Bot` to run `--mode all`, while keeping `VoiceClonePro-NativeWorker` disabled).
* **Command:** `powershell.exe`
* **Arguments:** `-NoProfile -ExecutionPolicy Bypass -Command "& 'C:\Users\awint\VoiceCloneProOfficial\.venv\Scripts\python.exe' main.py --mode all *>> 'C:\Users\awint\VoiceCloneProOfficial\logs\service.log'"`
* **Working Directory:** `C:\Users\awint\VoiceCloneProOfficial`

---

### D. Task `\VoiceClonePro-Cleanup`

* **Proposed State:** **Disable immediately in the cutover phase.**
* **Rationale:** The legacy cleanup script `run_cleanup.ps1` expects PostgreSQL tables (`media_files`) that do not exist in the official SQLite schema. Running it against the legacy workspace is useless, and pointing it to `VoiceCloneProOfficial` will fail because `VoiceCloneProOfficial` performs job-scoped cleanup inline.
* **Proposed Command:** `schtasks /change /tn "VoiceClonePro-Cleanup" /disable`

---

## 6. Prioritized Cutover Risks & Blockers

| Priority | Identifier | Category | Description | Mitigation Strategy |
|:---:|:---:|:---:|---|---|
| **P0** | OPS-01 | Scheduled Tasks | Tasks point to `VOICE_CLONE_PRO_Production_Final` with Docker/PostgreSQL assumptions. | Execute the cutover plan to re-point working directory and executable to `VoiceCloneProOfficial`. |
| **P1** | LOG-01 | Logging | Non-interactive background execution under Task Scheduler loses stdout/stderr if unredirected. | Ensure `logs\` directory exists and task command appends stdout/stderr to `logs\bot.log` and `logs\worker.log`. |
| **P1** | DB-01 | SQLite Concurrency | Running two independent OS processes (`bot` and `worker`) against the same SQLite file under high load. | Recommended: run `--mode all` in a single task, or configure a safe SQLite busy timeout (`PRAGMA busy_timeout=15000`). |
| **P2** | CLN-01 | Legacy Task | `VoiceClonePro-Cleanup` currently runs daily at 4:15 AM against legacy workspace. | Explicitly disable `VoiceClonePro-Cleanup` during the cutover phase. |
| **P3** | SEC-01 | Service Identity | Running as `SYSTEM` vs interactive user `awint`. | Keep `SYSTEM` (which has verified full access to project and `C:\Applio`), or use `awint` with saved credentials. |

---

## 7. Database Cutover Safety Assessment

* **Migration Check:** `VoiceCloneProOfficial` does not use Alembic migrations. At startup, `init_db()` runs `Base.metadata.create_all`, which is purely additive (`CREATE TABLE IF NOT EXISTS`).
* **Existing Data Safety:** If `data/voice_clone_official.db` already exists with tables, `Base.metadata.create_all` performs zero mutations or alterations.
* **Backup Prerequisite:** Before any service is launched against production data, a WAL-safe online backup must be created:
  ```powershell
  python -c "import sqlite3, datetime; ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S'); src = sqlite3.connect('file:data/voice_clone_official.db?mode=ro', uri=True); dst = sqlite3.connect(f'data/backups/voice_clone_official_{ts}.db'); src.backup(dst); dst.close(); src.close(); print('Backup complete.')"
  ```

---

## 8. Exact Post-Change Verification Checklist (For Future Phase)

During the future separately authorized cutover execution phase, verify:
1. `schtasks /query /tn "VoiceClonePro-Bot" /fo list /v` reflects:
   - Task To Run: Points to `C:\Users\awint\VoiceCloneProOfficial`
   - Start In: `C:\Users\awint\VoiceCloneProOfficial`
   - Status: Remains `Disabled` until manual smoke test passes.
2. `schtasks /query /tn "VoiceClonePro-NativeWorker" /fo list /v` reflects:
   - Task To Run: Points to `C:\Users\awint\VoiceCloneProOfficial`
   - Start In: `C:\Users\awint\VoiceCloneProOfficial`
   - Status: Remains `Disabled` until manual smoke test passes.
3. `schtasks /query /tn "VoiceClonePro-Cleanup" /fo list` reflects:
   - Status: `Disabled`
4. Directory `C:\Users\awint\VoiceCloneProOfficial\logs` exists.
5. Manual execution test: run `main.py --mode bot` in a supervised terminal session for 30 seconds; verify clean initialization and zero startup exceptions.

---

## 9. Rollback Plan

If any unexpected behavior occurs after updating task definitions, the tasks can be restored to their exact original legacy configuration using the captured XML backups:

### Rollback Definitions
* **`VoiceClonePro-Bot` Original Action:**
  - Command: `powershell.exe`
  - Arguments: `-NoProfile -ExecutionPolicy Bypass -File "C:\Users\awint\VOICE_CLONE_PRO_Production_Final\scripts\run_bot.ps1"`
  - Working Directory: `C:\Users\awint\VOICE_CLONE_PRO_Production_Final`
* **`VoiceClonePro-NativeWorker` Original Action:**
  - Command: `powershell.exe`
  - Arguments: `-NoProfile -ExecutionPolicy Bypass -File "C:\Users\awint\VOICE_CLONE_PRO_Production_Final\scripts\run_worker.ps1"`
  - Working Directory: `C:\Users\awint\VOICE_CLONE_PRO_Production_Final`
* **`VoiceClonePro-Cleanup` Original Action:**
  - Command: `powershell.exe`
  - Arguments: `-NoProfile -ExecutionPolicy Bypass -File "C:\Users\awint\VOICE_CLONE_PRO_Production_Final\scripts\run_cleanup.ps1"`
  - Working Directory: `C:\Users\awint\VOICE_CLONE_PRO_Production_Final`
  - Status: Re-enabled (`schtasks /change /tn "VoiceClonePro-Cleanup" /enable`)

---

## 10. Audit Integrity and Compliance Statement

* **Zero Tasks Modified:** Neither `VoiceClonePro-Bot`, `VoiceClonePro-NativeWorker`, nor `VoiceClonePro-Cleanup` was edited, enabled, disabled, registered, or unregistered.
* **Zero Services Started:** Bot polling, audio worker loop, and Applio subprocesses were NOT launched.
* **Zero Application Code Changes:** No source files, test files, or `.env` configuration files were modified.
* **Database Untouched:** No database connections, migrations, or write queries were performed against persistent storage.
* **Legacy Workspace Isolated:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` remains completely untouched.
