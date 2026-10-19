# Phase D13-B Evidence Report: Controlled Admin-Only Live Smoke Test (Option B)

**Phase:** D13-B — Controlled Admin-Only Live Smoke Test  
**Workspace:** `C:\Users\awint\VoiceCloneProOfficial`  
**Legacy Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` *(Strictly Read-Only — Untouched)*  
**Execution Timestamp:** 2026-10-12T18:46:45+06:30  
**Auditor / Roles:** Senior Python Engineer & Release Auditor  
**Operator Scope Authorization:** Corrected Option B (Telegram Bot Commands Menu Check + Exactly One Audio Test)  

---

## 1. Executive Summary & Status Overview

### Overall Phase Verdict: **PARTIAL PASS / DEFECT RECORDED (Telegram Bot Commands Menu Not Configured)**

In accordance with the operator's corrected Option B authorization instructions:
1. **Preflight & Backup Verification (PASS):** Verified 0 active Python processes, valid configuration, and an existing intact WAL-safe backup (`data/backups/voice_clone_official_pre_d13b_20261012_114954.db`, 143,360 bytes, integrity `ok`).
2. **Single Poller Startup (PASS):** Exactly one bot instance (`python main.py --mode all`) was launched. Database initialization executed cleanly (`Base.metadata.create_all` provisioned missing broadcast tables without mutating existing tables). Aiogram long polling started on `@VoiceCloneProMM_Bot` (`id=8907098555`).
3. **Telegram Commands Menu Verification (FAIL / DEFECT RECORDED):**  
   - The operator required verifying that `/start` and `/admin` appear in the **Telegram Bot Commands Menu key beside the message input field**.
   - Inspection via the Telegram Bot API (`bot.get_my_commands()`) confirmed that **no bot commands are registered** either globally (`BotCommandScopeDefault`) or specifically for the admin chat (`BotCommandScopeChat(admin_id)`).
   - While message handlers for `/start` and `/admin` exist and respond when manually typed, the application has no startup call to `bot.set_my_commands(...)`. Consequently, the native Commands Menu button in Telegram is empty.
   - Per the explicit instruction (*"If the expected Admin Dashboard command is missing, incorrectly exposed, or cannot be verified, record the finding and stop that part of the test rather than improvising a fix"*), this defect was formally recorded and no improvisation was attempted.
4. **Audio Generation Gate (STOPPED PER SPECIFICATION):**  
   - Per the test instruction (*"After the command-menu checks pass, generate exactly one short audio output... If the expected Admin Dashboard command is missing... stop that part of the test"*), audio generation was not triggered.
5. **Clean Shutdown & Integrity (PASS):**  
   - The bot process was gracefully stopped. Confirmed 0 active Python processes remain.
   - Database `PRAGMA integrity_check` verified `ok`.
   - Table counts verified: 0 broadcast campaigns, 0 deliveries, 0 minute debits, 0 ledger events.

---

## 2. Check-by-Check Verification Matrix

| # | Check Item | Verdict | Observed Fact vs Specification |
|---|---|:---:|---|
| **1** | **WAL-Safe SQLite Backup** | **PASS** | Pre-existing backup `voice_clone_official_pre_d13b_20261012_114954.db` (143,360 bytes) verified readable, untouched, and passed `PRAGMA integrity_check`. |
| **2** | **Single Poller Startup & Process Gate** | **PASS** | Confirmed 0 Python processes before start. Started exactly 1 instance with `main.py --mode all`. Polling active on `@VoiceCloneProMM_Bot`. |
| **3** | **Schema Auto-Provisioning on Boot** | **PASS** | `init_db()` executed `Base.metadata.create_all`. Tables `broadcast_campaigns` and `broadcast_deliveries` created automatically. Existing 12 tables and rows preserved. |
| **4** | **Telegram Bot Commands Menu (`/start`)** | **FAIL** | Telegram server returns empty list (`[]`) for `BotCommandScopeDefault`. `/start` is not registered in the native Bot Commands Menu key beside the input field. |
| **5** | **Telegram Bot Commands Menu (`/admin`)** | **FAIL** | Telegram server returns empty list (`[]`) for `BotCommandScopeChat(admin_id)`. The Admin Dashboard command is **missing** from the native Commands Menu. |
| **6** | **Stop-on-Defect Rule Enforcement** | **PASS** | Per instructions, audio generation was not improvised or forced after command-menu failure. That part of the test was stopped as directed. |
| **7** | **Audio Generation & Delivery** | **STOPPED** | Not performed due to Command Menu gate failure. Job count remains unchanged at 3. |
| **8** | **Admin Minute Exemption & Ledger Protection** | **PASS** | Admin balance remained `0s`. Zero minute ledger events created. Zero debit leakage. |
| **9** | **Broadcast Prohibition Gate** | **PASS (EXCLUDED)** | Absolutely no broadcast functions invoked. Confirmed `broadcast_campaigns` count = 0, `broadcast_deliveries` count = 0. |
| **10**| **Graceful Shutdown & Clean State** | **PASS** | Service cancelled cleanly. Verified 0 Python processes remaining. |
| **11**| **Post-Test Database Integrity** | **PASS** | Source DB `PRAGMA integrity_check` returned `ok`. Backup DB `PRAGMA integrity_check` returned `ok`. |
| **12**| **Legacy Workspace Isolation** | **PASS** | `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` verified completely untouched and strictly read-only. |

---

## 3. Detailed Investigation of Commands Menu Finding

### Observed Defect:
- **Requirement:** Admin opens the Telegram bot's Commands Menu key beside the message input field, verifies `/start` is listed, and verifies `/admin` (or Admin Dashboard) is exposed specifically for the authorized administrator.
- **Investigation Output via Telegram Bot API:**
  ```python
  default_cmds = await bot.get_my_commands(scope=BotCommandScopeDefault())
  # Result: []

  chat_cmds = await bot.get_my_commands(scope=BotCommandScopeChat(chat_id=admin_id))
  # Result: []
  ```
- **Root Cause Analysis:**
  1. The bot codebase contains valid message handlers:
     - [`app/handlers/start.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/handlers/start.py): `@start_router.message(CommandStart())`
     - [`app/handlers/admin.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/handlers/admin.py): `@admin_router.message(Command("admin"))`
  2. However, nowhere in the startup sequence ([`main.py`](file:///C:/Users/awint/VoiceCloneProOfficial/main.py)) or handler registration is `bot.set_my_commands(...)` invoked to publish bot commands to Telegram's UI button.
  3. Consequently, the Telegram client does not display `/start` or `/admin` in the pop-up menu button beside the message input field.
- **Action Taken:** Strictly followed the instruction: *"Do not change the command menu configuration or application code during this live test. If the expected Admin Dashboard command is missing, incorrectly exposed, or cannot be verified, record the finding and stop that part of the test rather than improvising a fix."*

---

## 4. Database Comparisons (Pre-Test vs Post-Test)

### Table Row Counts

| Table Name | Pre-Test Count | Post-Test Count | Delta | Explanation / Verification |
|---|:---:|:---:|:---:|---|
| **`audit_logs`** | 4 | 4 | 0 | Unchanged. No new audit log generated (audio generation held). |
| **`bot_settings`** | 0 | 0 | 0 | Unchanged. |
| **`broadcast_campaigns`** | *Table Absent* | **0** | **0** | Table auto-provisioned by `init_db()`. Zero campaigns created. |
| **`broadcast_deliveries`** | *Table Absent* | **0** | **0** | Table auto-provisioned by `init_db()`. Zero deliveries created. |
| **`jobs`** | 3 | 3 | 0 | Unchanged. Zero jobs created. |
| **`minute_balances`** | 1 | 1 | 0 | Admin balance unchanged (`welcome=0s, referral=0s, purchased=0s, consumed=0s`). |
| **`minute_ledger_events`** | 0 | 0 | 0 | Unchanged. Zero ledger debits or refunds. |
| **`payment_requests`** | 0 | 0 | 0 | Unchanged. |
| **`pricing_plans`** | 0 | 0 | 0 | Unchanged. |
| **`referral_rewards`** | 0 | 0 | 0 | Unchanged. |
| **`referrals`** | 0 | 0 | 0 | Unchanged. |
| **`users`** | 1 | 1 | 0 | Single admin user account preserved. |
| **`voice_models`** | 2 | 2 | 0 | Preserved (`တွတ်ပီ` ACTIVE, `အကိုလေး` READY). |
| **`welcome_bonus_awards`** | 0 | 0 | 0 | Unchanged. |

---

## 5. Post-Shutdown Verification & Environmental State

- **Process State:** **0** active Python processes.
- **Database Path:** [`data/voice_clone_official.db`](file:///C:/Users/awint/VoiceCloneProOfficial/data/voice_clone_official.db) (143,360 bytes, `PRAGMA integrity_check = ok`).
- **Backup Path:** [`data/backups/voice_clone_official_pre_d13b_20261012_114954.db`](file:///C:/Users/awint/VoiceCloneProOfficial/data/backups/voice_clone_official_pre_d13b_20261012_114954.db) (143,360 bytes, `PRAGMA integrity_check = ok`).
- **Git Working Tree:** Preserved without uncommitted code changes or staged files.
- **Legacy Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` verified untouched and strictly read-only.

---

## 6. Recommendations for Next Steps

1. **Commands Menu Configuration Fix (Required Before Retest):**
   - Add a startup setup function (e.g. in `main.py` or a dedicated setup hook) to register Telegram commands via the Bot API:
     - Global default scope (`BotCommandScopeDefault`): `/start` (Start Voice Studio), `/help` (Help & Guide), `/language` (Change Language).
     - Admin-specific chat scope (`BotCommandScopeChat(chat_id=admin_id)`): Add `/admin` (Admin Dashboard) exclusively for the configured administrator ID.
2. **Retest Gate:**
   - Once the Commands Menu registration is implemented in the codebase and verified with unit tests, re-run Phase D13-B to complete the full Option B test cycle (Commands Menu verification + single audio generation).
