# Phase D8 — Controlled Live Smoke Test: Preflight and Execution Gate (Stage 1 Report)

**Date:** 2026-10-12  
**Target Workspace:** `C:\Users\awint\VoiceCloneProOfficial`  
**Legacy Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` *(Strictly Read-Only — Untouched)*  
**Current State:** Stage 1 Read-Only Preflight Complete — Services NOT Started.  

---

## 1. Preflight Status Summary (Checks 1–7)

| # | Check Item | Status | Verification Detail |
|---|---|:---:|---|
| **1** | Git State & Working-Tree Diff | **PASS** | Branch `master`, no commits yet (initial clean codebase). Untracked files preserved. No unstaged/staged deletions. |
| **2** | Startup, Config & Shutdown Inspection | **PASS** | `main.py`, `app/handlers/__init__.py`, `app/worker.py` inspected. Startup command, boot sweep, polling, and clean cancellation verified. |
| **3** | Database Backup Readiness & WAL/SHM | **PASS** | Target `data/voice_clone_official.db` (143,360 bytes). No dangling WAL/SHM files. Safe atomic backup procedure documented. |
| **4** | Configuration & Connectivity Prerequisites | **PASS** | `.env` valid. Bot token configured (46 chars, redacted). Telegram API connectivity confirmed (`@VoiceCloneProMM_Bot`). Applio runtime confirmed (`C:\Applio`). Secrets redacted. |
| **5** | Process Status Check | **PASS** | `Get-CimInstance Win32_Process` confirms **0** active Python, bot, or worker processes running. |
| **6** | Previous Live Test Evidence Review | **PASS** | Reviewed Phase D5-C (admin generation) and Phase D6-L2 (progress UI). Identified unverified live aspects: full Burmese localization, language switching, non-admin ledger debit. |
| **7** | Minimal Test Matrix & Risk Classification | **PASS** | Proposed 3-tier controlled test matrix (Safe Admin Tests vs Controlled User Debit vs Excluded Destructive Tests). |

---

## 2. Git State and Working-Tree Summary

- **Branch:** `master`
- **HEAD:** No commits yet (initial project tree).
- **Working Tree:**
  - Preserved untracked components: `.env.example`, `.gitignore`, `app/`, `locales/`, `main.py`, `phase_d7_f_hardening_report.md`, `tests/`, `worker/`.
  - Zero working-tree modifications discarded or reset.
- **Legacy Workspace (`C:\Users\awint\VOICE_CLONE_PRO_Production_Final`):** Strictly untouched and unmodified.

---

## 3. Database Backup Readiness & Safe Backup Procedure

- **Database Path:** `C:\Users\awint\VoiceCloneProOfficial\data\voice_clone_official.db`
- **File Size:** 143,360 bytes
- **Journal Mode:** Write-Ahead Logging (`WAL`), set via connection pragma.
- **Current WAL State:** Closed cleanly; `data/voice_clone_official.db-wal` and `data/voice_clone_official.db-shm` are currently absent.
- **Safe Atomic Backup Procedure:**
  Before starting services or executing live test requests, create a consistent snapshot using SQLite's native Online Backup API:
  ```powershell
  .\.venv\Scripts\python.exe -c "
  import sqlite3, os
  os.makedirs('data/backups', exist_ok=True)
  src = sqlite3.connect('data/voice_clone_official.db')
  dst = sqlite3.connect('data/backups/voice_clone_official_pre_d8.db')
  src.backup(dst)
  dst.close()
  src.close()
  print('Atomic SQLite backup created successfully.')
  "
  ```
  This guarantees an uncorrupted, transaction-consistent snapshot regardless of WAL checkpoints.

---

## 4. Configuration & Connectivity Prerequisites (Redacted)

| Component | Target / Value | Status |
|---|---|:---:|
| **App Environment** | `development` | Verified |
| **Database Target** | `sqlite+aiosqlite:///./data/voice_clone_official.db` | Verified |
| **Telegram Bot Token** | Configured (Length: 46, Prefix: `8907...`, Suffix: `...c5q4`) | Verified |
| **Telegram API Connectivity** | Reached `api.telegram.org`: Bot `@VoiceCloneProMM_Bot` (ID: `8907098555`) | Verified |
| **Authorized Admin ID** | 1 Admin configured (ID: `19***15`) | Verified |
| **Channel Gate ID** | Configured (`-100...`) | Verified |
| **Group Gate ID** | Configured (`-100...`) | Verified |
| **Applio Workdir** | `C:\Applio` (Exists: `True`) | Verified |
| **Applio Python** | `C:\Applio\env\python.exe` (Exists: `True`) | Verified |
| **Applio Runner** | `worker\applio_runner.py` (Exists: `True`) | Verified |
| **Active Voice Model** | Model ID 1: `တွတ်ပီ` (`ACTIVE`, `.pth` file verified on disk) | Verified |
| **Ready Voice Model** | Model ID 2: `အကိုလေး` (`READY`, `.pth` file verified on disk) | Verified |

---

## 5. Current Process Status

- **Running Python Processes:** **0**
- **Existing Bot Polling:** **None**
- **Existing Audio Workers:** **None**
- **Port / Lock Conflicts:** None detected.

---

## 6. Review of Previous Live Evidence & Verification Gaps

1. **Previous Tests (D5-C & D6-L2):**
   - Verified that Edge TTS + Applio RVC can run end-to-end on Windows.
   - Verified that the animated stepper updates progress without spamming messages.
   - Verified that the progress ticker cancels cleanly on completion.
2. **Current Verification Gaps (To test in Phase D8):**
   - **Gap A:** Localized UI text across user navigation (Burmese default vs English switcher).
   - **Gap B:** Localized stepper stages and localized final delivery captions (added in Phase D7).
   - **Gap C:** HTML escaping safety under live Telegram Markdown/HTML parser (hardened in Phase D7-F).
   - **Gap D:** Dual account behavior: Admin exemption (0 balance allowed, no deduction) vs controlled normal user debit.

---

## 7. Minimal Test Matrix & Risk Classification

### 7.1 Risk Classification

- 🟢 **SAFE (Admin Execution):**
  - Testing `/start`, language selection, and main menu from the authorized Admin account (`19***15`).
  - Generating 1 short test audio (`တွတ်ပီ` model).
  - **Risk:** Zero impact on billing or user balance. Admin has unlimited access exemption.

- 🟡 **CONTROLLED (Normal User Account):**
  - Requires a second, non-admin Telegram account to verify welcome bonus (300s) and generation debit.
  - **Risk:** Debits usable seconds for that specific test account. Safe if run on an internal tester account.

- 🔴 **EXCLUDED / HIGH RISK:**
  - Simulating unhandled crashes during live processing on operator accounts.
  - Modifying live pricing or triggering real bank transfers.
  - *Recommendation:* Keep live testing strictly to the non-destructive test protocol below.

---

## 8. Proposed Step-by-Step Live Test Protocol

If explicit authorization is granted:

1. **Step 1 — Create Safe Database Backup:**
   Execute the atomic backup script to create `data/backups/voice_clone_official_pre_d8.db`.
2. **Step 2 — Launch Single Official Instance:**
   Execute `.\.venv\Scripts\python.exe main.py --mode all` in an observable background session.
3. **Step 3 — Operator Telegram Interaction (Admin Account):**
   - Send `/start` to `@VoiceCloneProMM_Bot`.
   - Verify Myanmar welcome screen and 4-button menu:
     `🎙️ Text → Voice → Clone`, `💳 မိနစ်ဝယ်ယူရန်`, `🎁 သူငယ်ချင်းဖိတ်ခေါ်ရန်`, `👤 အကောင့် / လက်ကျန်မိနစ်`.
   - Open Voice Studio (`/clone`). Select model `တွတ်ပီ`.
   - Submit short Burmese sentence: `မင်္ဂလာပါ voice clone pro စမ်းသပ်မှု ဖြစ်ပါသည်။`
   - Observe live stepper:
     - Stage 1: `စစ်ဆေးခြင်း`
     - Stage 2: `Base Audio (Edge TTS)`
     - Stage 3: `AI Voice Conversion (Applio)` with live spinner & elapsed timer
     - Stage 4: `အချောသတ် ပြင်ဆင်ခြင်းနှင့် ပေးပို့ခြင်း`
   - Confirm delivery of single audio file with localized Burmese caption:
     `✅ အသံဖိုင် အောင်မြင်စွာ ဖန်တီးပြီးပါပြီ`
   - Confirm Admin balance remains 0 without deduction or error.
4. **Step 4 — Clean Shutdown:**
   Send graceful interrupt (`SIGINT` / `Ctrl+C` / task termination) to the running process and verify clean shutdown in logs.
5. **Step 5 — Post-Test Database Inspection:**
   Verify `jobs` row incremented to 3, `minute_ledger_events` remains 0 for admin, and database integrity intact.

---

## 9. Rollback & Emergency Shutdown Procedure

- **Emergency Stop:** If any abnormal behavior occurs (polling loop, repeated message editing, Telegram FloodWait), immediately kill the background task process ID.
- **Rollback:** In the event of unintended database mutation, restore `data/backups/voice_clone_official_pre_d8.db` to `data/voice_clone_official.db`.

---

## 10. Execution Gate & Request for Approval

Stage 1 preflight checks are 100% complete and verified.

**Services have NOT been started. No live Telegram messages or jobs have been sent.**

Awaiting explicit operator authorization to proceed with the controlled live smoke test.
