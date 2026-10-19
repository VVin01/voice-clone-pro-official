# Phase D14-A — Admin User Management & Safe Minute Adjustments Evidence Report

**Workspace:** `C:\Users\awint\VoiceCloneProOfficial`  
**Legacy Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` (Strictly read-only; untouched)  
**Date:** 2026-10-12  
**Status:** COMPLETE & VERIFIED (All 123 tests passing)

---

## 1. Executive Summary

Phase D14-A implements the approved admin capability enhancements for **VoiceCloneProOfficial** strictly adhering to the product's fundamental core model: **selling voice-generation minutes, not subscription plans or account tiers**.

### Scope Delivered:
1. **Admin User Management**:
   - Paginated user list (5 users per page) with Prev/Next navigation, page clamping, and safe handling for empty databases.
   - Comprehensive user detail card rendering Telegram ID, username, first name, account status (`FREE` or `PURCHASED`), usable and categorized minute balances (purchased, welcome, referral, consumed), and latest 3 ledger events.
   - Strict HTML escaping on all dynamic user-provided fields (usernames, names, ledger notes) to prevent Telegram HTML parse failures or injection vulnerabilities.
2. **Safe Manual Minute Adjustments**:
   - Controlled 3-step FSM workflow (`AdminAdjustFSM`): Direction selection (`CREDIT` / `DEBIT`) → Whole-minute positive amount input → Mandatory reason input (>= 3 non-whitespace chars).
   - Dynamic preview card displaying target user, proposed delta, and projected balance.
   - Nonce-protected confirmation button (`admin:adjust_confirm:<token>`) ensuring explicit operator intent and preventing replay/double-click.
   - Instant cancellation (`admin:adjust_cancel`) available at all stages, safely clearing state and returning to user view.
   - Database hardening in `admin_adjust_balance`: Row-locking (`with_for_update()`), positive amount validation, strict debit bounds checking (cannot debit more than existing purchased seconds), status category auto-transition (`FREE` <-> `PURCHASED`), idempotency key suppression, and transactional creation of `MinuteLedgerEvent(source=ADJUST)` and `AuditLog`.
3. **Queue Stale Job Recovery Trigger**:
   - Internal Queue Control menu (`admin:queue`) with safe recovery prompt (`admin:queue_recover_prompt`) and explicit confirmation (`admin:queue_recover_confirm`).
   - Safely invokes existing `recover_stale_running_jobs(session, bot)` and alerts the admin of recovered job count.
4. **Localization & Terminology Parity**:
   - Eradicated misleading terminology ("tier", "VIP/PRO", "Pricing Plans") in English and Burmese locales (`locales/en/messages.json`, `locales/my/messages.json`).
   - Replaced with canonical terminology: "Active Minute Packages" and "Account Type / Status (`FREE` / `PURCHASED`)".
5. **Native Bot Commands Menu Preservation**:
   - Verified that `BotCommandScopeDefault` continues to register **strictly `/start`** per Phase D8 Stage 2A specifications.

---

## 2. Architecture & File Inventory

### Modified & Created Files in `VoiceCloneProOfficial`:

| File | Changes Made |
| :--- | :--- |
| `app/services/ledger.py` | Added `get_paginated_users()`, `get_user_recent_ledger_events()`. Hardened `admin_adjust_balance()` with row-locking, debit checks, category updates, idempotency checks, ledger events, and audit logging. |
| `app/ui/keyboards.py` | Added `admin_users_list_kb()`, `admin_user_card_kb()`, `admin_adjust_cancel_kb()`, `admin_adjust_preview_kb()`, `admin_queue_kb()`, `admin_queue_recover_confirm_kb()`. Updated terminology in `admin_menu_kb()`. |
| `app/handlers/admin.py` | Added `AdminAdjustFSM`, user pagination handlers (`handle_admin_users`, `handle_admin_users_page`, `render_admin_users_page`), user detail card (`handle_admin_user_detail`), complete adjustment FSM handlers (`handle_admin_adjust_start`, `handle_admin_adjust_amount_input`, `handle_admin_adjust_reason_input`, `handle_admin_adjust_confirm`, `handle_admin_adjust_cancel`), and queue recovery handlers (`handle_admin_queue`, `handle_admin_queue_recover_prompt`, `handle_admin_queue_recover_confirm`). |
| `locales/en/messages.json` | Removed "tier" references and updated pricing titles to "Minute Packages". |
| `locales/my/messages.json` | Updated Burmese titles to match Minute Packages terminology. |
| `tests/unit/test_admin_handlers.py` | Updated button text assertion to `"Active Minute Packages"`. |
| `tests/unit/test_admin_user_management.py` | **NEW** comprehensive unit test suite with 15 test cases covering pagination, card escaping, ledger adjustments, FSM validations, token confirmation, cancellation, queue recovery, authorization, and terminology parity. |

---

## 3. Implementation Details

### 3.1. Paginated User List & User Detail Card
- Query uses `select(User).order_by(User.created_at.desc())` with `offset((page - 1) * page_size).limit(page_size)` and `select(func.count(User.id))`.
- Each user item button displays: `@{username} · {FREE|PURCHASED} · {total_m}m`.
- Previous/Next navigation buttons are dynamically generated based on current page and total pages.
- Dynamic data in the detail card is safely escaped using Python's standard `html.escape()`:
  ```python
  safe_username = f"@{html.escape(target_user.username)}" if target_user.username else "<i>None</i>"
  safe_first_name = html.escape(target_user.first_name) if target_user.first_name else "<i>None</i>"
  # Ledger events:
  safe_note = html.escape(ev.note or "No note")
  ```

### 3.2. Safe Manual Minute Adjustments
- **Debit Bounds Check**:
  ```python
  if direction == LedgerDirection.DEBIT:
      if balance.purchased_seconds < seconds:
          raise ValueError(
              f"Cannot debit {seconds}s ({seconds/60:.1f}m). User only has {balance.purchased_seconds}s ({balance.purchased_minutes:.1f}m) purchased seconds."
          )
      balance.purchased_seconds -= seconds
  ```
- **Category Promotion & Demotion**:
  - Crediting purchased seconds auto-promotes `account_category` to `PURCHASED`.
  - Debiting down to 0 total usable seconds auto-demotes `account_category` to `FREE`.
- **Idempotency Protection**:
  - When an `idempotency_key` is supplied, `admin_adjust_balance` checks if a `MinuteLedgerEvent` with `reference_id == idempotency_key` already exists. If found, the current balance is returned immediately without double-applying the adjustment.
- **Audit Trail**:
  - Every adjustment records both a `MinuteLedgerEvent` (`source=ADJUST`) and an `AuditLog` (`action="ADMIN_BALANCE_ADJUST"`).

### 3.3. Queue Stale Job Recovery Flow
- Triggered exclusively by authorized admin via `admin:queue_recover_prompt`.
- Presents a warning dialogue explaining that stale running jobs exceeding timeout will be marked failed, temporary files cleaned, and user minute balances safely refunded.
- Explicit confirmation callback `admin:queue_recover_confirm` executes `recover_stale_running_jobs(session, bot)` and displays an alert indicating how many stale jobs were cleaned up.

---

## 4. Test Verification & Results

### 4.1. Focused Test Suite: `tests/unit/test_admin_user_management.py`
Command: `pytest tests/unit/test_admin_user_management.py -v`
Result: **15 passed in 7.68s**

```
tests/unit/test_admin_user_management.py::test_admin_users_pagination_and_navigation PASSED [  6%]
tests/unit/test_admin_user_management.py::test_admin_users_empty_db PASSED [ 13%]
tests/unit/test_admin_user_management.py::test_admin_user_detail_card_and_html_escaping PASSED [ 20%]
tests/unit/test_admin_user_management.py::test_admin_user_detail_deleted_or_missing PASSED [ 26%]
tests/unit/test_admin_adjust_balance_credit_success_and_status_promotion PASSED [ 33%]
tests/unit/test_admin_adjust_balance_debit_success_and_status_demotion PASSED [ 40%]
tests/unit/test_admin_adjust_balance_insufficient_debit_rejected PASSED [ 46%]
tests/unit/test_admin_adjust_balance_validation_errors PASSED [ 53%]
tests/unit/test_admin_adjust_balance_idempotency_duplicate_suppression PASSED [ 60%]
tests/unit/test_admin_adjust_fsm_workflow_complete PASSED [ 66%]
tests/unit/test_admin_adjust_fsm_validation_failures PASSED [ 73%]
tests/unit/test_admin_adjust_fsm_cancel_and_expired_token PASSED [ 80%]
tests/unit/test_admin_queue_recovery_flow PASSED [ 86%]
tests/unit/test_unauthorized_admin_user_management PASSED [ 93%]
tests/unit/test_localization_parity_and_terminology PASSED [100%]
```

### 4.2. Full Regression Suite: `pytest -v`
Command: `pytest -v`
Result: **123 passed in 16.65s**

```
============================ 123 passed in 16.65s =============================
```
- Total test files: 16 test modules across unit, UX, ledger, broadcast, worker, crash recovery, and security suites.
- Failures: **0**
- Errors: **0**

---

## 5. Safety Compliance & Verification Checklist

| Safety Requirement | Status | Verification Evidence |
| :--- | :---: | :--- |
| **Minutes-Only Model Preserved** | ✅ PASS | Strictly `FREE` and `PURCHASED` account categories. No subscription tiers or recurring billing. |
| **Telegram Commands Menu Intact** | ✅ PASS | Verified `test_setup_bot_commands_registers_only_start_in_default_scope` PASSED. `BotCommandScopeDefault` has only `/start`. |
| **Admin Authorization Enforced** | ✅ PASS | Verified `test_unauthorized_admin_user_management` PASSED. Non-admin IDs rejected on all callbacks. |
| **HTML Escaping** | ✅ PASS | Verified `test_admin_user_detail_card_and_html_escaping` PASSED. Special characters (`<`, `>`, `&`) safely sanitized. |
| **Offline Safety Maintained** | ✅ PASS | 0 bot instances started; 0 Telegram polling or network calls made; 0 audio models invoked. |
| **Production Database Unchanged** | ✅ PASS | `data/voice_clone_official.db` was not modified; no DDL or migrations executed. |
| **Legacy Workspace Read-Only** | ✅ PASS | `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` was completely untouched. |
| **Secrets & Token Redaction** | ✅ PASS | Zero bot tokens, API keys, or secrets leaked into logs or artifacts. |

---

## 6. Conclusion

Phase D14-A is fully implemented, thoroughly tested, and ready for operator review. All user management, manual minute adjustment, queue recovery, and localization tasks were executed cleanly with 100% test passing rate.
