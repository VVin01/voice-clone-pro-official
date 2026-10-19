# Phase D19 — Identity-Only User Migration Plan & Preflight Report (NO IMPORT)

**Official Target Workspace:** `C:\Users\awint\VoiceCloneProOfficial`  
**Legacy Source Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` *(Strictly Read-Only — Untouched)*  
**Date:** 2026-10-13  
**Auditor / Migration Engineer:** Senior Python, Database & Release Reliability Engineer  

---

## 1. Final Preflight Verdict

### **VERDICT: READY FOR EXPLICIT MIGRATION APPROVAL**

The preflight audit confirms that an identity-only migration of the 77 non-admin legacy users into the official SQLite database is technically ready, architecturally sound, and fully isolated from business data.
- **Zero Database Writes**: Neither database was altered. Zero rows were imported.
- **Administrator Protection**: Existing official Administrator record (`1957002115`) is protected from collision via `INSERT OR IGNORE` and explicit exclusion.
- **Zero Phantom Credits**: All 77 imported users receive cleanly zeroed `minute_balances` records (`welcome_seconds = 0`, `purchased_seconds = 0`).
- **Code Behavior Confirmed**: Rigorous code inspection of `app/handlers/start.py`, `app/services/gate.py`, and `app/services/ledger.py` proves:
  - **Welcome Bonus**: Migrated users **will correctly receive** their 5-minute welcome bonus upon `/start` and channel/group verification.
  - **Membership Gate**: Migrated users **will NOT bypass** channel and group checks.
  - **Language Selection**: Existing-user `/start` skips the language prompt and uses imported `language_code` (`my` or `en`).
  - **Bot Token Discrepancy Identified**: Official `.env` currently contains Bot ID `8907098555` while legacy users chatted with Bot ID `8631167018`. To maintain continuous chat identity, the token must be aligned before production launch.

---

## 2. Code-Level Onboarding & Lifecycle Proofs

### 2.1. Welcome Bonus Lifecycle Verification
A critical audit question was whether imported users would miss their welcome bonus.
- **The Trigger Path**:
  In `app/services/gate.py` (lines 60–63):
  ```python
  # Award welcome bonus once (for eligible non-admin users)
  if not settings.is_admin(user.id):
      await award_welcome_bonus(session, user.id)
  ```
  `verify_user_membership()` is executed unconditionally in `app/handlers/start.py` (line 49) whenever an existing user sends `/start`, as well as when clicking `gate:verify`.
- **The Award Guard**:
  In `app/services/ledger.py` (lines 153–179):
  ```python
  if user.welcome_bonus_awarded:
      return False, balance
  ```
- **Conclusion**:
  Because all 77 users will be imported with `welcome_bonus_awarded = False`, the guard passes. When the user sends `/start` and their channel and group memberships are confirmed, `award_welcome_bonus()` successfully:
  1. Inserts a `WelcomeBonusAward` database record.
  2. Sets `user.welcome_bonus_awarded = True`.
  3. Increments `balance.welcome_seconds += 300` (5.0 minutes).
  4. Records an immutable `MinuteLedgerEvent` (`source='WELCOME'`, `direction='CREDIT'`, `seconds=300`).
  **Migrated users will NOT miss their welcome bonus.**

### 2.2. Membership Gate Verification
Does an existing user bypass channel and group verification?
- In `app/handlers/start.py` (lines 46–60):
  ```python
  lang = user.language_code

  # Check gate
  is_verified, _ = await verify_user_membership(session, user, message.bot)
  await session.commit()

  if not is_verified:
      text = f"{t('membership_gate_title', lang)}\n\n{t('membership_gate_desc', lang)}"
      kb = membership_gate_kb(...)
      await message.answer(text, reply_markup=kb, parse_mode="HTML")
      return
  ```
- **Conclusion**:
  `verify_user_membership()` queries Telegram API for both `required_channel_id` and `required_group_id`. If either check fails, the user is halted at the gate and shown the join links.
  **Migrated users CANNOT bypass membership checks.**

### 2.3. Language Selection Behavior (Audit Finding & Smallest Safe Recommendation)
In `app/handlers/start.py` (lines 38–44):
```python
if is_new:
    # Let user choose language first
    await message.answer(
        t("welcome_choose_lang", "my"),
        reply_markup=language_selection_kb(),
    )
    return
```
- **Observed Behavior**:
  Because imported users are found in `users`, `get_or_create_user()` returns `is_new = False`.
  Consequently, the bot skips `welcome_choose_lang` and defaults to `user.language_code`.
  - For 75 users, their legacy language (`my` or `en`) is preserved.
  - For the 3 users with empty legacy language, they are defaulted to `'my'`.
- **User Impact**:
  Users do not see the language picker on `/start`, but can switch languages at any time using `/language` or `/lang`.
- **Smallest Safe Fix (If Operator Requires Mandatory Language Selection)**:
  *Do not implement now.* If the operator decides all migrated users must explicitly choose their language upon first connecting to the official release, the smallest safe modification in `app/handlers/start.py` would be:
  ```python
  # Instead of strictly checking `is_new`, check if the user has completed official gate verification:
  if is_new or (not user.channel_verified and not user.welcome_bonus_awarded):
      await message.answer(t("welcome_choose_lang", "my"), reply_markup=language_selection_kb())
      return
  ```
  This cleanly prompts language selection on first `/start` without altering database schema.

---

## 3. Telegram Bot Token & Identity Assessment

- **Audit Requirement**: Confirm the same Telegram bot identity/token assumption without exposing secrets.
- **Evidence Gathered**:
  - Legacy `.env`: `TELEGRAM_BOT_TOKEN` prefix is `8631167018`.
  - Official `.env`: `TELEGRAM_BOT_TOKEN` prefix is `8907098555`.
  - Exact token match: **`False`** (Two distinct Telegram bots).
- **Critical Architectural Consequence**:
  1. The 78 legacy users hold chat history with Bot `8631167018`.
  2. If the official bot launches using Bot `8907098555`, Telegram Bot API policies prevent the bot from initiating messages or broadcasts to those users until each user independently opens and `/start`s Bot `8907098555`.
  3. **Operational Requirement**: For migrated users to maintain continuous, frictionless bot access, the operator must configure the official bot's `.env` with the production token (`8631167018`) at launch.

---

## 4. Constraint & Integrity Validations

1. **`referral_code` Uniqueness**:
   - SQLite schema enforces `referral_code: VARCHAR(32) UNIQUE NULL`.
   - Migration policy assigns `f"ref_{telegram_id}"` for each imported user.
   - All 77 legacy Telegram IDs are distinct from each other and distinct from Admin (`1957002115`), guaranteeing zero unique constraint collisions.
2. **Foreign-Key Dependencies**:
   - `users.id` is the primary key and Telegram ID.
   - `minute_balances.user_id` references `users.id` with `ON DELETE CASCADE`.
   - The migration transaction inserts into `users` first, followed immediately by corresponding zeroed records into `minute_balances`.
3. **Timestamp Compatibility**:
   - Legacy PostgreSQL timestamps are timezone-aware UTC (`timestamptz`).
   - SQLite stores timestamps as formatted UTC ISO-8601 strings (`YYYY-MM-DD HH:MM:SS.ffffff`). Python `datetime.isoformat()` ensures full SQLAlchemy `DateTime(timezone=True)` parsing compatibility.
4. **Idempotency & Rollback**:
   - The import script wraps all inserts inside `BEGIN IMMEDIATE` / `COMMIT`.
   - Uses `INSERT OR IGNORE` filtering on `id NOT IN (SELECT id FROM users)`.
   - Re-running the script results in 0 inserts and 0 mutations.

---

## 5. Implementation-Ready Migration Plan

### 5.1. Execution Prerequisites
1. **Host Verification**: Confirm 0 Python bot or worker processes are running (`Get-Process python`).
2. **Database State**: Confirm official database is idle and uncorrupted (`PRAGMA integrity_check`).
3. **Preflight Backup**: Execute atomic online SQLite backup before any writes:
   ```powershell
   python -c "import sqlite3, datetime; ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S'); src = sqlite3.connect('file:data/voice_clone_official.db?mode=ro', uri=True); dst = sqlite3.connect(f'data/backups/voice_clone_official_pre_migration_{ts}.db'); src.backup(dst); dst.close(); src.close(); print('Backup complete.')"
   ```

### 5.2. Atomic Import Script Specification (`scripts/import_legacy_users.py`)
```python
"""Single-transaction, identity-only legacy user import script."""
import sqlite3
import datetime
import asyncpg
from dotenv import dotenv_values
from pathlib import Path

# 1. Connect to PostgreSQL source in read-only mode
legacy_env = dotenv_values(Path(r"C:\Users\awint\VOICE_CLONE_PRO_Production_Final\.env"))
dsn = legacy_env.get("DATABASE_URL").replace("postgresql+asyncpg://", "postgresql://")

# 2. Extract 77 non-admin records
# Query: SELECT telegram_id, username, first_name, language_code, created_at, updated_at
#        FROM users WHERE telegram_id != 1957002115 ORDER BY id

# 3. Connect to SQLite target data/voice_clone_official.db
# conn = sqlite3.connect("data/voice_clone_official.db")
# conn.execute("PRAGMA foreign_keys = ON")
# conn.execute("BEGIN IMMEDIATE")
#
# Insert into users:
#   id = telegram_id
#   username = username
#   first_name = first_name
#   language_code = language_code if language_code in ('my', 'en') else 'my'
#   account_category = 'FREE'
#   status = 'ACTIVE'
#   channel_verified = 0
#   group_verified = 0
#   welcome_bonus_awarded = 0
#   referral_code = f"ref_{telegram_id}"
#   created_at = created_at_iso
#   updated_at = updated_at_iso
#
# Insert into minute_balances:
#   user_id = telegram_id
#   welcome_seconds = 0
#   referral_seconds = 0
#   purchased_seconds = 0
#   consumed_seconds = 0
#   updated_at = updated_at_iso
#
# conn.commit()
```

### 5.3. Post-Import Verification Queries
The operator or script will immediately run the following SQLite queries:
1. `PRAGMA integrity_check;` $\rightarrow$ Must return `ok`.
2. `PRAGMA foreign_key_check;` $\rightarrow$ Must return `[]` (0 violations).
3. `SELECT count(*) FROM users;` $\rightarrow$ Must return exactly `78`.
4. `SELECT count(*) FROM minute_balances;` $\rightarrow$ Must return exactly `78`.
5. `SELECT * FROM users WHERE id = 1957002115;` $\rightarrow$ Must be identical to pre-migration Admin row.
6. `SELECT count(*) FROM users WHERE welcome_bonus_awarded = 1;` $\rightarrow$ Must return `0`.
7. `SELECT count(*) FROM minute_balances WHERE welcome_seconds > 0;` $\rightarrow$ Must return `0`.

### 5.4. Rollback Procedure
If any verification query fails or an unexpected exception occurs:
1. Discard the active transaction (`conn.rollback()`).
2. If committed, restore from the preflight backup:
   ```powershell
   Copy-Item data/backups/voice_clone_official_pre_migration_<ts>.db data/voice_clone_official.db -Force
   Remove-Item data/voice_clone_official.db-wal, data/voice_clone_official.db-shm -ErrorAction SilentlyContinue
   ```

---

## 6. Preflight Checklist Summary

| Verification Item | Preflight Verdict | Notes |
| :--- | :---: | :--- |
| **Source Data Isolation** | **PASS** | Only 6 identity fields mapped; all 12 business tables dropped. |
| **Admin Collision Safety** | **PASS** | ID `1957002115` explicitly filtered; Admin row remains untouched. |
| **Referral Code Integrity** | **PASS** | Deterministic `ref_<telegram_id>` guarantees 100% uniqueness. |
| **Foreign-Key Parity** | **PASS** | Every new user receives paired 0-balance row in `minute_balances`. |
| **Welcome Bonus Delivery** | **PASS** | Code inspection confirms 5-minute grant triggers upon gate join. |
| **Membership Gate Enforcement** | **PASS** | Code inspection confirms gate is enforced on all existing users. |
| **Bot Token Alignment** | **ACTION REQUIRED** | Operator must align `.env` token to production identity before launch. |
| **Read-Only Compliance** | **PASS** | 0 records written, 0 files staged, working trees untouched. |

---

## 7. Next Step

This phase is strictly a preflight plan. **Zero database mutations have occurred.**  
Awaiting explicit operator authorization to execute the migration plan.
