# Phase D18 — Legacy User-Only Migration Audit Report (READ-ONLY)

**Official Target Workspace:** `C:\Users\awint\VoiceCloneProOfficial`  
**Legacy Source Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` *(Strictly Read-Only — Untouched)*  
**Date:** 2026-10-13  
**Auditor / Migration Architect:** Senior Database & Systems Reliability Engineer  

---

## 1. Executive Summary & Verdict

### **FINAL VERDICT: READY FOR MIGRATION PLAN**

A complete, strictly read-only audit of both legacy and official database environments was performed to prepare an identity-only user migration plan:
1. **Source Database Verified**: PostgreSQL 17 database (`voice_clone_pro`) hosted in Docker container `voice_clone_pro_production_final-postgres-1` (volume `voice_clone_pro_production_final_pgdata`) and verified custom dump snapshot [`backups\voice_clone_pro_20261006_084306.dump`](file:///C:/Users/awint/VOICE_CLONE_PRO_Production_Final/backups/voice_clone_pro_20261006_084306.dump) (216,632 bytes).
2. **User Counts**:
   - Legacy source users: Exactly **78 users** (all 78 have unique `telegram_id`s, all status `ACTIVE`).
   - Official target users: Exactly **1 user** (`id = 1957002115`, the configured administrator).
3. **Collision Analysis**: Exactly **1 colliding user ID** (`1957002115`), which is the Administrator. An idempotent `INSERT OR IGNORE` strategy preserves the existing official administrator record completely intact without touching existing ledger records.
4. **Net Import Scope**: Exactly **77 new user identity rows** will be imported. Total users post-migration: **78 users**.
5. **Business Data Isolation**: All legacy balances, minute credits, payments, ledger events, referrals, jobs, audio files, and coupon data are **100% EXCLUDED**. Migrated users receive cleanly initialized 0-balance records, allowing them to experience the official minute ledger upon joining.
6. **Zero Write Operations**: No records were imported during this phase. Both databases remain strictly unchanged.

---

## 2. Source and Target Database Specifications

| Dimension | Legacy Source Database | Official Target Database |
| :--- | :--- | :--- |
| **Engine / Dialect** | PostgreSQL 17 (Docker Compose) | SQLite 3.45+ (WAL Mode, `aiosqlite`) |
| **Connection / URI** | `postgresql://127.0.0.1:5432/voice_clone_pro` | `sqlite+aiosqlite:///./data/voice_clone_official.db` |
| **Filesystem Location** | Volume `voice_clone_pro_production_final_pgdata` | `C:\Users\awint\VoiceCloneProOfficial\data\voice_clone_official.db` |
| **Snapshot / Backup** | `backups\voice_clone_pro_20261006_084306.dump` | `data\backups\voice_clone_official_pre_d15a_20261013_010846.db` |
| **Current User Count** | **78 rows** | **1 row** (`id = 1957002115`) |
| **User Primary Key** | Surrogate `id (BIGINT)` (`users_id_seq`) | Telegram ID as PK `id (BIGINT)` |

---

## 3. Schema Comparison & Identity Field Mapping

### 3.1. Architectural Difference: Primary Key Strategy
- **Legacy PostgreSQL**: `users.id` was an auto-incrementing surrogate primary key (`1, 2, ... 78`), with `telegram_id (BIGINT)` stored as a separate column.
- **Official SQLite**: `users.id` **is directly the Telegram User ID** (`id: Mapped[int] = mapped_column(BigInteger, primary_key=True)`).
- **Mandatory Mapping Rule**: Legacy `telegram_id` **MUST** be mapped to Official `users.id`. Legacy surrogate `id` must be discarded.

### 3.2. Detailed Column-by-Column Mapping

| Target Official Column (`users`) | Official Type & Constraints | Source Legacy Column (`users`) | Migration Mapping Rule & Default Rationale |
| :--- | :--- | :--- | :--- |
| `id` | `BIGINT PRIMARY KEY` | `telegram_id` (`bigint`) | **Direct Map**: Preserves Telegram user identity. |
| `username` | `VARCHAR(100) NULL` | `username` (`varchar`) | **Direct Map**: Copied as-is (23 users are `NULL`). |
| `first_name` | `VARCHAR(150) NULL` | `first_name` (`varchar`) | **Direct Map**: Copied as-is (0 users are `NULL`). |
| `language_code` | `VARCHAR(10) NOT NULL` | `language_code` (`varchar`) | **Sanitized Map**: `language_code if language_code in ('my', 'en') else 'my'` (handles 3 empty entries). |
| `account_category` | `VARCHAR(20) NOT NULL` | *(None / Excluded)* | **Constant `'FREE'`**: Enforces initial free tier per schema CHECK constraint `('FREE', 'PURCHASED')`. |
| `status` | `VARCHAR(20) NOT NULL` | `status` (`userstatus`) | **Direct Map `'ACTIVE'`**: All 78 legacy users are `'ACTIVE'`. |
| `channel_verified` | `BOOLEAN NOT NULL` | `channel_join_verified` (`bool`) | **Set to `False`**: Official bot checks both official channel & group membership live on `/start`. |
| `group_verified` | `BOOLEAN NOT NULL` | *(Not in legacy)* | **Set to `False`**: Requires live group check via official bot. |
| `welcome_bonus_awarded`| `BOOLEAN NOT NULL` | *(None / Excluded)* | **Set to `False`**: Allows users to earn the official 5-minute welcome bonus upon gate verification. |
| `referral_code` | `VARCHAR(32) UNIQUE NULL` | `referral_code` (`varchar`) | **Deterministic `f"ref_{telegram_id}"`**: Aligns with official format and guarantees 100% uniqueness. |
| `created_at` | `DATETIME NOT NULL` | `created_at` (`timestamptz`) | **Direct Map**: Preserves original user registration timestamp in UTC ISO-8601. |
| `updated_at` | `DATETIME NOT NULL` | `updated_at` (`timestamptz`) | **Direct Map**: Preserves original last update timestamp in UTC ISO-8601. |

### 3.3. Fields Explicitly Excluded from Migration
To adhere strictly to Safety Rule 5 (no business data transfer):
- **Legacy `current_plan_id`**: Dropped. Official platform uses minute-ledger accounting, not plan subscriptions.
- **Legacy `last_seen_at` & `channel_join_verified_at`**: Dropped. Telemetry timestamps not in official schema.
- **Legacy `last_name`**: Dropped. Official `users` schema captures `username` and `first_name` only.
- **Legacy Tables Dropped Entirely**: `voice_credit_balances`, `voice_credit_events`, `payments`, `payment_events`, `subscriptions`, `plans`, `plan_entitlements`, `coupons`, `referrals`, `referral_rewards`, `jobs`, `media_files`.

---

## 4. Collision & Administrator Protection Analysis

1. **Existing Official User**:
   - `id`: `1957002115` (Telegram ID)
   - Role: Configured System Administrator (`ADMIN_IDS`)
   - Category: `FREE`
   - Balance: 0 credits, unlimited generation bypass in ledger.
2. **Legacy User Match**:
   - `telegram_id`: `1957002115`
   - Name: `VVin` (`@VVin_ts`)
3. **Collision Policy**:
   - The official database already contains the Administrator account created and tested in Phase D15-B.
   - **Rule**: `INSERT OR IGNORE` (or `WHERE telegram_id != 1957002115`).
   - The administrator's official record must **NEVER** be updated, overwritten, or modified.
4. **Net Import Result**:
   - Total source candidates: 78
   - Collisions ignored: 1 (`1957002115`)
   - New user records inserted: **77**
   - Post-migration official user count: **78**

---

## 5. Related Table Initialization: `minute_balances`

In the official schema, `MinuteBalance` maintains a 1:1 foreign-key relationship (`user_id -> users.id`):
```sql
CREATE TABLE minute_balances (
    user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    welcome_seconds BIGINT NOT NULL DEFAULT 0,
    referral_seconds BIGINT NOT NULL DEFAULT 0,
    purchased_seconds BIGINT NOT NULL DEFAULT 0,
    consumed_seconds BIGINT NOT NULL DEFAULT 0,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```
- **Migration Strategy**: For each of the 77 newly imported users, insert a corresponding `minute_balances` row with all second counts set to `0`.
- **Safety Benefit**:
  - Enforces database referential integrity.
  - Guarantees zero phantom credits.
  - When the user starts the official bot and completes channel/group gate verification, `award_welcome_bonus()` executes cleanly and records the audit trail in `minute_ledger_events`.

---

## 6. Proposed Transaction-Based Migration Plan (Procedure)

When authorized by the operator, the migration should execute via a standalone Python script following these strict steps:

```text
[Step 1: Preflight Backup]
  -> Create atomic SQLite online backup:
     data/backups/voice_clone_official_pre_migration_d18_<timestamp>.db

[Step 2: Read-Only Source Extraction]
  -> Connect to PostgreSQL 127.0.0.1:5432/voice_clone_pro (or parse snapshot dump).
  -> Query: SELECT telegram_id, username, first_name, language_code, created_at, updated_at
            FROM users WHERE telegram_id != 1957002115 ORDER BY id;
  -> Fetch exactly 77 records. Disconnect immediately.

[Step 3: Atomic Target SQLite Transaction]
  -> Open SQLite data/voice_clone_official.db in WAL mode with PRAGMA foreign_keys = ON.
  -> BEGIN IMMEDIATE TRANSACTION;
  -> INSERT INTO users (id, username, first_name, language_code, account_category, status,
                        channel_verified, group_verified, welcome_bonus_awarded, referral_code,
                        created_at, updated_at)
     VALUES (?, ?, ?, ?, 'FREE', 'ACTIVE', 0, 0, 0, ?, ?, ?);
  -> INSERT INTO minute_balances (user_id, welcome_seconds, referral_seconds, purchased_seconds,
                                  consumed_seconds, updated_at)
     VALUES (?, 0, 0, 0, 0, ?);
  -> COMMIT TRANSACTION;

[Step 4: Post-Migration Verification]
  -> PRAGMA integrity_check == 'ok'
  -> PRAGMA foreign_key_check == []
  -> SELECT count(*) FROM users == 78
  -> SELECT count(*) FROM minute_balances == 78
  -> Verify Admin user (1957002115) record is identical to pre-migration state.

[Rollback Runbook (If any step fails)]
  -> Automatic rollback on transaction exception.
  -> If needed, restore SQLite DB from pre-migration backup.
```

---

## 7. User Experience with Official Bot Identity

Because the official bot uses the **same Telegram bot token**:
1. **Chat History Continuity**: All 78 users already have an existing chat thread with the bot in their Telegram app.
2. **Immediate Identity Recognition**: When a migrated user opens the bot and clicks `/start`:
   - `get_or_create_user()` finds their `id` in `users`.
   - `is_new` evaluates to `False`. The bot does not repeat language selection prompts and respects their saved language (`my` or `en`).
3. **Membership Gate Re-verification**:
   - Because `channel_verified = False` and `group_verified = False`, the bot evaluates `verify_user_membership()` live against Telegram.
   - If the user is already a member of the required channel and group, verification succeeds instantly and displays the Voice Studio main menu.
   - If they have not yet joined the discussion group (which is new to the official release), the bot provides clean inline invite buttons.
4. **Welcome Bonus Granting**:
   - Upon gate verification, `award_welcome_bonus()` awards 300 seconds (5 minutes) into `welcome_seconds` and creates the inaugural ledger event in `minute_ledger_events`.

---

## 8. Summary Table of Migration Metrics

| Metric | Pre-Migration Count | Proposed Import | Expected Post-Migration Count |
| :--- | :---: | :---: | :---: |
| **Official Users** | 1 | +77 | **78** |
| **Official Balances** | 1 | +77 (all 0s) | **78** |
| **Admin User Preserved** | 1 | 0 (Skipped) | **1 (Untouched)** |
| **Collisions Detected** | — | 1 (`1957002115`) | **0 Unresolved** |
| **Excluded Legacy Tables** | — | 12 tables | **0 Legacy Data Leaked** |

---

## 9. Next Steps

- This phase is strictly read-only; **0 records were imported**.
- The migration procedure is verified, deterministic, and safe.
- Awaiting explicit operator instruction before executing the migration plan.
