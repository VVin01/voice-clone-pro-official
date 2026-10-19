# Phase D14-A-Fix-2 — Narrow Idempotency Error Handling Evidence Report

**Workspace:** `C:\Users\awint\VoiceCloneProOfficial`  
**Legacy Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` (Strictly read-only; untouched)  
**Date:** 2026-10-12  
**Implementation Status:** **COMPLETE & VERIFIED**  
**Verdict:** **PASS**

---

## 1. Executive Summary

This phase remediated finding **F1 (MEDIUM)** identified during the independent review of Phase D14-A-Fix. 

Prior to this fix, `admin_adjust_balance` contained a blanket `except IntegrityError:` handler around the nested savepoint. If any constraint violation other than an idempotency primary-key collision occurred (for example, a foreign key failure, check constraint violation, or unexpected database-level integrity error), the exception would be caught and silently treated as a duplicate idempotency replay, returning the current balance without surfacing the error.

### Resolved Behavior:
1. **Narrowed Exception Discrimination**: After the savepoint rolls back on `IntegrityError`, a fresh database read via `session.get(AdjustmentIdempotencyRecord, idempotency_key, populate_existing=True)` is performed.
2. **Duplicate Suppression Only on Genuine Confirmation**: Only when the exact `AdjustmentIdempotencyRecord` is confirmed to exist in the database (committed by a racing session) does the method suppress the error and return the existing balance.
3. **Re-raising Unexpected Errors**: If no matching `AdjustmentIdempotencyRecord` exists, the original `IntegrityError` is re-raised unchanged.
4. **No-Key Safety**: If `idempotency_key` is `None` or empty, any `IntegrityError` is unconditionally re-raised.
5. **Session Usability**: Because the error occurs within `session.begin_nested()`, the savepoint rollback leaves the outer transaction and session in a clean, usable state.

---

## 2. Changed Files & Implementation Details

### 2.1. [`app/services/ledger.py`](file:///C:/Users/awint/VoiceCloneProOfficial/app/services/ledger.py)
In `admin_adjust_balance` (lines 604–639):

```python
    except IntegrityError as exc:
        # The savepoint has been rolled back automatically by begin_nested().__aexit__.
        # Determine whether this IntegrityError is a confirmed idempotency-key PK collision
        # or an unexpected constraint failure (FK violation, CHECK constraint, etc.).
        #
        # After savepoint rollback the outer session is still usable. We perform a fresh
        # SELECT to see whether a committed AdjustmentIdempotencyRecord for this key exists.
        # If it does:  a racing session already applied the adjustment → suppress replay.
        # If it does not: some other constraint was violated → re-raise so the caller
        #                 and the outer transaction handle it correctly.
        if idempotency_key:
            # Use populate_existing=True so the identity map returns a freshly-loaded row
            # rather than any stale cached object that was expired by the savepoint rollback.
            confirmed_rec = await session.get(
                AdjustmentIdempotencyRecord,
                idempotency_key,
                populate_existing=True,
            )
            if confirmed_rec is not None:
                logger.info(
                    "Idempotency PK collision confirmed for key %s (user %s); "
                    "suppressing duplicate adjustment.",
                    idempotency_key, user_id,
                )
                await session.expire(user)
                await session.expire(balance)
                return await get_minute_balance(session, user_id)

        # Not a confirmed idempotency collision — re-raise the original exception unchanged
        # so FK violations, CHECK constraints, and other unexpected errors are not swallowed.
        logger.error(
            "Unexpected IntegrityError during balance adjustment (user=%s, key=%s): %s",
            user_id, idempotency_key, exc,
        )
        raise
```

### 2.2. [`tests/unit/test_admin_idempotency_fix.py`](file:///C:/Users/awint/VoiceCloneProOfficial/tests/unit/test_admin_idempotency_fix.py)
Created focused test module testing against isolated temporary SQLite databases:
- `test_sequential_duplicate_suppressed`: Proves sequential replay with same key returns existing balance without double-crediting.
- `test_concurrent_duplicate_suppressed`: Proves concurrent requests across separate database sessions via `asyncio.gather` result in exactly one credit applied.
- `test_non_idempotency_integrity_error_propagated`: Proves non-idempotency `IntegrityError` (e.g. unique constraint or foreign key violation) is propagated and leaves zero mutations in balance, ledger, or audit log.
- `test_distinct_keys_allow_separate_adjustments`: Proves distinct idempotency keys apply both adjustments independently.
- `test_no_success_before_commit`: Proves balance adjustments remain uncommitted if caller rolls back the transaction.
- `test_integrity_error_without_key_always_reraised`: Proves when no key is supplied (`idempotency_key=None`), any `IntegrityError` is always re-raised.

---

## 3. Test Execution Results

### 3.1. Focused Tests
```bash
python -m pytest tests/unit/test_admin_idempotency_fix.py -v
```
**Output:**
```
tests/unit/test_admin_idempotency_fix.py::test_sequential_duplicate_suppressed PASSED [ 16%]
tests/unit/test_admin_idempotency_fix.py::test_concurrent_duplicate_suppressed PASSED [ 33%]
tests/unit/test_admin_idempotency_fix.py::test_non_idempotency_integrity_error_propagated PASSED [ 50%]
tests/unit/test_admin_idempotency_fix.py::test_distinct_keys_allow_separate_adjustments PASSED [ 66%]
tests/unit/test_admin_idempotency_fix.py::test_no_success_before_commit PASSED [ 83%]
tests/unit/test_admin_idempotency_fix.py::test_integrity_error_without_key_always_reraised PASSED [100%]

============================== 6 passed in 1.39s ==============================
```

### 3.2. Core Domain Group Tests
```bash
python -m pytest tests/unit/test_admin_idempotency_fix.py tests/unit/test_admin_user_management.py tests/unit/test_ledger.py tests/unit/test_crash_recovery.py tests/unit/test_bot_commands.py tests/unit/test_i18n.py -v
```
**Output:**
```
============================= 49 passed in 11.54s =============================
```

### 3.3. Full Regression Suite
```bash
python -m pytest -v
```
**Output:**
```
============================ 132 passed in 18.81s =============================
```
*(All 132 tests in the repository pass cleanly).*

---

## 4. Production Database Safety & Schema Status

- **Database Path:** `data/voice_clone_official.db`
- **Integrity Check:** `PRAGMA integrity_check` returned `[('ok',)]`.
- **Schema State:** `adjustment_idempotency_records` table does not exist in `data/voice_clone_official.db`.
- **Safety Gate:** No migrations or write operations were executed against the production database.
- **Legacy Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` verified untouched (read-only).

---

## 5. Remaining Risks & Operational Notes

1. **Production Table Creation Precondition**:  
   As observed in D14-A-Fix-R review finding F2, in development and test environments `Base.metadata.create_all` automatically creates `adjustment_idempotency_records`. In an existing production database where auto-creation is not invoked dynamically, `migrations/013_adjustment_idempotency.sql` must be applied prior to serving admin adjustments to avoid table-missing errors during adjustment execution.
2. **Offline Mode**:  
   All validations were completed strictly offline. Zero live Telegram API calls, background worker processes, or audio generations were performed.
