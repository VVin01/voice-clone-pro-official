# VoiceCloneProOfficial V2 — Admin Dashboard & Minutes Integrity Remediation Report

**Date:** 2026-10-14  
**Project Directory:** `C:\Users\awint\VoiceCloneProOfficial`  
**Interpreter:** `C:\Users\awint\AppData\Local\Programs\Python\Python310\python.exe`  
**Target Database:** `data/voice_clone_v2.db` (`db_identity = voice_clone_v2`, size: `233,472` bytes)  
**Legacy Database:** `data/voice_clone_official.db` (Preserved read-only, size: `192,512` bytes)  
**Running Bot Process:** PID `3148` (Uninterrupted)  

---

## 1. Executive Summary & Root Cause Investigation

### 1.1 Proven Root Cause of the Unexpected 8-Minute Balance
During post-cutover live testing on Free account `8582483248` (`@Milo_N4U`), the user profile displayed `8.0m` (480s) usable allowance instead of the expected `3.0m` (180s) monthly free allowance.

A strict read-only audit of `data/voice_clone_v2.db` and runtime call graphs revealed the exact cause:

1. **First Interaction (`/start`):**
   - [`app/handlers/start.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/handlers/start.py) called `grant_monthly_free_allowance(session, user_id)`.
   - This recorded Ledger Event #1: `(1, 'CREDIT', 'MONTHLY_FREE', 180, 'Monthly Free Allowance 2026-10 (3 minutes)')`.
   - Cumulative balance: **180s (3.0 minutes)**.

2. **Gate Verification & Language Selection:**
   - In [`app/services/gate.py#L62`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/gate.py#L62), `verify_user_membership()` unconditionally invoked `award_welcome_bonus(session, user.id)` upon verifying channel and group membership.
   - `award_welcome_bonus()` in [`app/services/ledger.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/ledger.py) was hardcoded to award **300s (5.0 minutes)** with source `WELCOME`.
   - This recorded Ledger Event #2: `(2, 'CREDIT', 'WELCOME', 300, 'Welcome Bonus 5 Minutes (Once-per-account)')`.
   - Cumulative balance: $180\text{s} + 300\text{s} = 480\text{s} = \mathbf{8.0\text{ \textbf{minutes}}}$.

3. **Subsequent Generation:**
   - User generated a 27-second audio clip, recorded as Ledger Event #3: `(3, 'DEBIT', 'CONSUME', 27, ...)`.
   - Current balance: `monthly_free_seconds = 153`, `welcome_seconds = 300`, `consumed_seconds = 27`, usable = **453s (7.55m)**.

### 1.2 What Was Ruled Out
- **Legacy Database Queries:** The application connects exclusively to `data/voice_clone_v2.db` via `settings.database_url`. The legacy database was never opened or queried by the V2 runtime.
- **Migration & Seeding Artifacts:** `audience_migration.py` imports only Telegram IDs and user profiles into `imported_audience`; it never seeds minute balances or awards bonuses.
- **Display Math / Float Bugs:** `total_usable_seconds` accurately summed `monthly_free_seconds + welcome_seconds + referral_seconds + purchased_seconds`. The database held real balance rows totaling 480 seconds.

---

## 2. Remediations Implemented & Architecture Decisions

### Area A: Consolidated Settings & Policies View
- **Problem:** The Admin Dashboard displayed separate `⚙️ Settings` and `⚙️ Policies` buttons that presented redundant, fragmented system information.
- **Remediation:**
  - Removed duplicate `⚙️ Policies` button from [`admin_dashboard_kb`](file:///C:/Users/awint/VoiceCloneProOfficial/app/ui/keyboards.py#L125).
  - Maintained canonical callback `admin:settings` with backward-compatible alias route `admin:policies`.
  - Consolidated into a unified **Bot Runtime Settings & System Policies** card displaying:
    - Infrastructure runtime: App name, environment, log level, TTS voice, Applio workdir, Applio python path, gate IDs.
    - Tier policies: Free monthly allowance, character limits, daily job limits, concurrent job limits, and model access rules.
    - Package pricing and deduction hierarchy (`monthly_free` ➔ `purchased`).

### Area B: Minutes Management & Ledger Audit
- **Problem:** Admins lacked a direct way to view historical ledger events for a user when inspecting minute balances.
- **Remediation:**
  - Added `📜 Ledger History` button on the user detail card (`admin:user_ledger:<user_id>:<page>`).
  - Implemented [`handle_admin_user_ledger`](file:///C:/Users/awint/VoiceCloneProOfficial/app/handlers/admin.py#L980) displaying the 10 most recent ledger events with UTC timestamp, direction (`CREDIT`/`DEBIT`), duration in minutes/seconds, source (`MONTHLY_FREE`, `WELCOME`, `PURCHASE`, `CONSUME`, `ADJUST`), job reference ID, and audit note.

### Area C: Payment Confirmation Safeguards
- **Problem:** Tapping "Approve" or "Reject" on pending payments previously mutated the database immediately without an intermediate safety prompt.
- **Remediation:**
  - Introduced two-step confirmation prompts:
    - `admin:pay_approve:<id>` ➔ `handle_admin_pay_approve_prompt` displaying target user, package, and amount, requiring explicit tap on `admin:pay_approve_confirm:<id>`.
    - `admin:pay_reject:<id>` ➔ `handle_admin_pay_reject_prompt` displaying rejection warning, requiring explicit tap on `admin:pay_reject_confirm:<id>`.
  - Both confirmation handlers revalidate payment status, atomically commit status updates, send localized user notifications, and write structured `AuditLog` records.

### Area D: Guide Video Management (Tutorial CRUD)
- **Problem:** Updating the tutorial video required manual `.env` file editing and process restarts.
- **Remediation:**
  - Backed tutorial state dynamically by `bot_settings` table keys `tutorial_video_link` and `tutorial_video_enabled`, falling back to `settings.tutorial_video_link` if unset.
  - Implemented full admin CRUD workflow:
    - View status badge (`🟢 Enabled` / `🔴 Disabled`) and current link.
    - Toggle visibility (`admin:tutorial_toggle`).
    - Edit URL (`admin:tutorial_edit_url` + `AdminTutorialFSM.waiting_for_url`) with `http://` / `https://` prefix validation.
    - Remove URL with confirmation (`admin:tutorial_remove_confirm`).
  - Updated user-facing [`handle_menu_tutorial`](file:///C:/Users/awint/VoiceCloneProOfficial/app/handlers/menu.py#L85): When disabled or cleared, gracefully renders `tutorial_temporarily_unavailable` notice and omits external link buttons.

### Area E: Queue Monitoring & Granular Failure Recovery
- **Problem:** Bulk queue recovery could dangerously retry non-retryable user errors (e.g., insufficient balance, invalid characters), causing repeated failures.
- **Remediation:**
  - Replaced bulk recovery with an individual failed-job recovery pipeline:
    - Updated `handle_admin_queue` to report counts across all 5 statuses (`QUEUED`, `RUNNING`, `SUCCESS`, `FAILED`, `CANCELLED`).
    - `admin:queue_failures` lists recent failed jobs with truncated error summaries.
    - `handle_admin_job_detail` classifies failures via `evaluate_job_retry_eligibility()` into `SYSTEM_FAILURE` (retryable) vs `POLICY_USER_ERROR` (non-retryable). Non-retryable keywords include `INSUFFICIENT_BALANCE`, `MODEL_NOT_AUTHORIZED`, `TEXT_EXCEEDS_`, `DAILY_JOB_LIMIT_EXCEEDED`, and `BLOCKED`.
    - Retry prompt (`admin:job_retry_prompt:<id>`) and confirmation (`admin:job_retry_confirm:<id>`) revalidate user state, reset status atomically to `QUEUED`, clear previous error message, and record an audit entry.
  - Retained `handle_admin_queue_recover_disabled` to reject legacy bulk callbacks safely.

### Area F: Default 0-Second Welcome Bonus
- **Remediation:**
  - Added `welcome_bonus_seconds: int = Field(default=0, alias="WELCOME_BONUS_SECONDS")` in [`app/config/settings.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/config/settings.py#L96).
  - Updated [`app/services/gate.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/gate.py#L62): Welcome bonus is awarded only if `settings.welcome_bonus_seconds > 0`.
  - Updated [`app/services/ledger.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/ledger.py#L125): `award_welcome_bonus` accepts optional `seconds` parameter and safely no-ops when `<= 0`.

---

## 3. Files Modified & Added

| File | Type | Changes |
|---|---|---|
| [`app/config/settings.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/config/settings.py) | Modified | Added `welcome_bonus_seconds = 0` default setting. |
| [`app/services/gate.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/gate.py) | Modified | Gated `award_welcome_bonus` with `welcome_bonus_seconds > 0`. |
| [`app/services/ledger.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/ledger.py) | Modified | Added `seconds` parameter to `award_welcome_bonus`; safely no-ops on `<= 0`. |
| [`app/ui/keyboards.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/ui/keyboards.py) | Modified | Streamlined dashboard to 5 rows; added payment confirmation, tutorial CRUD, and job retry keyboards. |
| [`app/handlers/admin.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/handlers/admin.py) | Modified | Consolidated Settings/Policies; added user ledger history; added payment approve/reject confirmation; added tutorial video CRUD; added job retry eligibility and retry handlers; added backward compatibility aliases. |
| [`app/handlers/menu.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/handlers/menu.py) | Modified | Updated `handle_menu_tutorial` to query dynamic `BotSetting` state with graceful fallback. |
| [`locales/en/messages.json`](file:///C:/Users/awint/VoiceCloneProOfficial/locales/en/messages.json) | Modified | Added English translation keys for payment confirmation, tutorial CRUD, and queue retry alerts. |
| [`locales/my/messages.json`](file:///C:/Users/awint/VoiceCloneProOfficial/locales/my/messages.json) | Modified | Added Myanmar translation keys maintaining 100% key parity. |
| [`tests/conftest.py`](file:///C:/Users/awint/VoiceCloneProOfficial/tests/conftest.py) | Modified | Added `welcome_bonus_seconds` to settings restore fixture. |
| [`tests/unit/test_telegram_ux_and_auth.py`](file:///C:/Users/awint/VoiceCloneProOfficial/tests/unit/test_telegram_ux_and_auth.py) | Modified | Explicitly scoped welcome bonus test fixture. |
| [`tests/unit/test_v2_admin_remediation.py`](file:///C:/Users/awint/VoiceCloneProOfficial/tests/unit/test_v2_admin_remediation.py) | Added | 7 comprehensive unit tests covering all remediation areas. |

---

## 4. Verification & Test Evidence

### 4.1 Focused Test Suite Execution
```powershell
& "C:\Users\awint\AppData\Local\Programs\Python\Python310\python.exe" -m pytest tests/unit/test_v2_admin_remediation.py -v
```
**Result:** `7 passed in 3.54s` (Exit Code `0`).

### 4.2 Full Unit Test Suite Execution
```powershell
& "C:\Users\awint\AppData\Local\Programs\Python\Python310\python.exe" -m pytest tests/unit/ -v
```
**Result:** `202 passed in 27.97s` (Exit Code `0`).  
**Pass Rate:** **100%** (202 passed, 0 failed, 0 errors across 27 test files).

### 4.3 Database Integrity Evidence
- **V2 Database:** `data/voice_clone_v2.db` exists, size `233,472` bytes.
  - `DatabaseMetadata`: `db_identity = 'voice_clone_v2'`, `schema_version = '2.0.0'`.
- **Legacy Database:** `data/voice_clone_official.db` exists, size `192,512` bytes (Preserved untouched).
- **Live Process:** PID `3148` running continuously without crashes or restarts.

---

## 5. Test User `8582483248` Balance & Proposed Remediation

### 5.1 Current Untouched State
In strict adherence to instructions, no production records for user `8582483248` were altered:
- **User:** `8582483248` (`@Milo_N4U`)
- **Category / Status:** `FREE` / `ACTIVE`
- **Balance Table:** `monthly_free_seconds = 153`, `welcome_seconds = 300`, `consumed_seconds = 27`, `purchased_seconds = 0`
- **Usable Allowance:** `453s (7.55m)`

### 5.2 Authorization-Gated Correction Script
To revert user `8582483248`'s balance to the intended Free allowance ($180 - 27 = 153\text{s} = 2.55\text{m}$), the following script is prepared and can be executed upon explicit owner authorization:

```python
# To be executed only upon explicit authorization:
import asyncio
from app.db.session import AsyncSessionLocal
from app.models.schema import MinuteBalance, MinuteLedgerEvent, WelcomeBonusAward

async def correct_test_user_balance():
    async with AsyncSessionLocal() as session:
        # Zero out the unentitled welcome bonus
        bal = await session.get(MinuteBalance, 8582483248)
        if bal and bal.welcome_seconds > 0:
            removed_seconds = bal.welcome_seconds
            bal.welcome_seconds = 0
            
            # Record compensating ledger event
            session.add(MinuteLedgerEvent(
                user_id=8582483248,
                direction="DEBIT",
                source="ADJUST",
                seconds=removed_seconds,
                note="Admin correction: Revoke unentitled V2 welcome bonus",
            ))
            
            # Remove welcome bonus record to allow clean state
            award = await session.get(WelcomeBonusAward, 8582483248)
            if award:
                await session.delete(award)
                
            await session.commit()
            print("Successfully adjusted balance for 8582483248: 153s remaining.")

# asyncio.run(correct_test_user_balance())
```

---

## 6. Summary of Safety Guard Compliance
1. **Voice Generation Flow Preserved:** No changes made to Edge TTS generation, audio normalization, or local Applio inference worker.
2. **Production Database Untouched for User 8582483248:** Verified via read-only queries.
3. **No Git Destruction:** No `git reset`, `git clean`, `git commit`, or `git push` executed.
4. **PID 3148 Preserved:** Bot process kept running undisturbed.
5. **100% Test Suite Pass:** 202/202 unit tests verified passing with Exit Code `0`.
