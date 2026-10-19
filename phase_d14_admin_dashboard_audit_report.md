# Phase D14 — Admin Dashboard Read-Only Audit & Scoped Improvements Report

**Audit Date:** 2026-10-12  
**Target Workspace:** `C:\Users\awint\VoiceCloneProOfficial`  
**Legacy Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` (*Read-Only, Untouched*)  
**Auditor:** Senior Python / SQLAlchemy / Telegram Bot Release Auditor  
**Audit Mode:** Strictly Read-Only  

---

## 1. Executive Summary

This audit evaluates the internal **Admin Dashboard** architecture in `VoiceCloneProOfficial` following the completion of Phases D8 through D13-B.

### Core Architectural Principles & Product Model
1. **Minute-Only Product Economy:**
   - The platform sells voice-generation **minutes** (converted internally to seconds), not recurring subscription packages, VIP/PRO memberships, or account tiers.
   - Account statuses are strictly binary: **`FREE`** and **`PURCHASED`**.
   - A user who purchases minutes receives `PURCHASED` account status (granting higher per-request character generation limits). When their purchased balance is consumed to zero, their account status automatically reverts to `FREE`.
2. **Commands Menu Boundary:**
   - The Telegram native Bot Commands Menu beside the text input field is strictly registered for `BotCommandScopeDefault` containing **only `/start`** (established in Phase D8 Stage 2A and verified in D13-B-Fix Correction).
   - Administrative commands (`/admin`) and auxiliary commands (`/help`, `/lang`) are **not** exposed in the native menu.
3. **Admin Dashboard Interface:**
   - The Admin Dashboard is an internal, in-chat administrative interface accessed via direct `/admin` text command or the inline keyboard button `admin:dashboard` on the `/start` menu for authorized administrators.
   - Server-side authorization (`is_authorized_admin`) independently protects every administrative message handler and callback query.

---

## 2. Evidence-Based Capability Matrix

| # | Capability Area | Actual Handlers, Services & Models | Authorization Boundary | Current Test Evidence | Missing Behavior & Limitations | Production Risk |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **1** | **Dashboard Metrics & Refresh / Navigation** | **Handler:** `app/handlers/admin.py` (`handle_admin_command`, `handle_admin_dashboard`, `render_admin_dashboard`, `_fetch_dashboard_metrics`)<br>**Models:** `User`, `MinuteBalance`, `Job`, `PaymentRequest`, `VoiceModel`<br>**UI:** `app/ui/keyboards.py` (`admin_dashboard_kb`) | `is_authorized_admin` independently checks `settings.is_admin(user_id)` on both message and callback. | `tests/unit/test_admin_handlers.py`<br>• `test_admin_command_handler_authorized`<br>• `test_admin_command_handler_unauthorized`<br>• `test_admin_dashboard_callback_refresh`<br>• `test_admin_dashboard_metrics_and_html_escaping`<br>• `test_admin_dashboard_empty_db_safety` | Live aggregated database counts. Sanitized HTML escaping for job error messages. Inline refresh handles Telegram message-not-modified safely. | **Low**.<br>Verified solid and resilient against empty databases and malformed error strings. |
| **2** | **Voice Model Inventory & Activation** | **Handler:** `app/handlers/admin.py` (`handle_admin_voices`, `handle_admin_model_scan`, `handle_admin_model_detail`, `handle_admin_model_toggle`, `handle_admin_model_free`)<br>**Services:** `app/services/models.py` (`scan_all_models`, `activate_model`, `disable_model`, `set_free_default_model`)<br>**Models:** `VoiceModel`, `AuditLog` | `is_authorized_admin` on all callbacks. | `tests/unit/test_model_catalog_and_scan.py`<br>`tests/unit/test_safe_model_activation.py`<br>`tests/unit/test_admin_handlers.py` (`test_admin_model_detail_html_escaping`) | Lists models with status badges (🟢 ACTIVE, 🟡 READY, ⚪ DISABLED, 🔴 BROKEN). Disk scan reconciles `.pth` and `.index` artifacts. Admin can toggle ACTIVE/DISABLED and set/unset free default. Safe audit logging.<br>*Missing:* No model file deletion via UI (by design to protect disk storage). | **Low**.<br>Fully verified with audit trails. |
| **3** | **Manual Payment Review & Minute Credits** | **Handler:** `app/handlers/admin.py` (`handle_admin_payments`, `handle_admin_pay_detail`, `handle_admin_pay_approve`, `handle_admin_pay_reject`)<br>**Services:** `app/services/payments.py` (`approve_payment_request`, `reject_payment_request`, `credit_purchased_minutes`)<br>**Models:** `PaymentRequest`, `MinuteBalance`, `MinuteLedgerEvent`, `AuditLog` | `is_authorized_admin` on each callback. | `tests/unit/test_payments.py`<br>• `test_pricing_plans_and_payment_lifecycle`<br>• `test_payment_rejection`<br>`tests/unit/test_admin_handlers.py`<br>• `test_admin_pay_reject_html_escaping` | Approves pending slip, credits exact minutes to `MinuteBalance.purchased_seconds`, writes ledger event with `source=PURCHASE` and payment ID reference, creates `AuditLog`, notifies user on Telegram. Rejection marks `PaymentRequest.status=REJECTED`.<br>*Limitations:*<br>1. Rejection immediately uses default reason `"Payment evidence unverified"`; no interactive FSM prompt to enter custom rejection reason.<br>2. In `handle_admin_pay_detail`, receipt photo is not re-displayed if reviewed later from menu (metadata text only). | **Moderate UX Limitation**.<br>Idempotency and transaction safety are verified (duplicate approval attempts return `ALREADY_APPROVED` and never double credit). |
| **4** | **Broadcast Lifecycle & Tracking** | **Handler:** `app/handlers/admin.py` (`handle_admin_broadcast_command`, `handle_admin_broadcast_start`, `handle_admin_broadcast_message_input`, `handle_admin_broadcast_cancel`, `handle_admin_broadcast_confirm`)<br>**Services:** `app/services/broadcast.py` (`create_or_get_broadcast_campaign`, `deliver_broadcast`, `resume_broadcast_campaign`)<br>**Models:** `BroadcastCampaign`, `BroadcastDelivery`, `User` | `is_authorized_admin` on message input, cancel, and confirm tokens. | `tests/unit/test_broadcast.py` (comprehensive 15-test suite covering rate limits, retry-after, forbidden blocking, token nonce expiration, intermediate progress) | FSM-based draft, HTML preview with recipient count, token confirmation nonce, automatic state clearing on send. Durable per-recipient delivery rows. Honors Telegram `retry_after` without capping at 5s. Auto-marks blocked users.<br>*Missing:* `resume_broadcast_campaign` exists in service layer, but there is no Telegram UI button or command in `admin.py` to inspect or resume interrupted campaigns. | **Low**.<br>Delivery engine and safety gates are thoroughly verified. |
| **5** | **User Search, Pagination & Account Visibility** | **Handler:** `app/handlers/admin.py` (`handle_admin_users` via `admin:users`)<br>**Models:** `User`, `MinuteBalance`, `Referral`, `ReferralReward` | `is_authorized_admin`. | None specific to user listing (only count assertions in general dashboard tests). | **Major Gap:** `handle_admin_users` only displays high-level aggregate counts (`total_users`, `free_users`, `purchased_users`, `total_referrals`, `total_rewards`) with a "Back" button.<br>*Missing:*<br>1. No user search by Telegram ID or `@username`.<br>2. No paginated user list.<br>3. No user detail card showing individual balances (`purchased_seconds`, `welcome_seconds`, `referral_seconds`, `consumed_seconds`).<br>4. No view of user ledger history or recent jobs. | **High Operational Limitation**.<br>Admins cannot look up a specific user's balance or account status during support inquiries. |
| **6** | **Manual Minute Adjustments & Ledger Trail** | **Service:** `app/services/ledger.py` (`admin_adjust_balance`)<br>**Models:** `MinuteBalance`, `MinuteLedgerEvent`, `AuditLog`, `User` | Service receives `admin_id`. | **Zero test coverage** across entire `tests/` directory. | **Major Gap:** Complete lack of Telegram UI.<br>1. `admin_adjust_balance` function exists in `ledger.py` and supports `CREDIT`/`DEBIT`, mandatory reason, `MinuteLedgerEvent` with `source=ADJUST`, and `AuditLog`.<br>2. **Zero** handlers, callbacks, or FSM dialogs exist in `app/handlers/admin.py` to trigger this function.<br>3. `admin_adjust_balance` currently lacks an idempotency check (`reference_id`) to prevent accidental double-submission. | **High Operational Limitation**.<br>Admins cannot issue compensation credits or manual balance corrections from Telegram. |
| **7** | **Queue Monitor & Stuck Job Recovery** | **Handler:** `app/handlers/admin.py` (`handle_admin_queue` via `admin:queue`)<br>**Services:** `app/services/recovery.py` (`recover_stale_running_jobs`), `app/services/queue.py`<br>**Models:** `Job`, `MinuteLedgerEvent`, `AuditLog` | `is_authorized_admin`. | `tests/unit/test_crash_recovery.py`<br>`tests/unit/test_queue_concurrency.py` | Shows queued, running, success, and failed counts.<br>*Missing:*<br>1. Read-only text display only; no list of currently queued or running jobs.<br>2. No detail view for failed jobs (only the single latest error is shown on main dashboard).<br>3. No manual "Recover Stale Jobs" button in Telegram (recovery only executes on worker startup). | **Moderate Operational Limitation**.<br>Automated crash recovery is solid, but admin has no interactive recovery trigger in Telegram. |
| **8** | **Settings & Tutorial URL Management** | **Handler:** `app/handlers/admin.py` (`handle_admin_settings`, `handle_admin_tutorial`)<br>**Config:** `app/config/settings.py` (`app_env`, character limits, Applio workdir, `tutorial_video_link`)<br>**Models:** `BotSetting` (table exists in schema, but unused) | `is_authorized_admin`. | `tests/unit/test_admin_handlers.py` (`test_admin_settings_handler_authorized`, `test_admin_tutorial_handler_authorized`) | Read-only inspection of runtime settings and video URL with a test URL button.<br>*Missing:* No dynamic editing of settings or video link from Telegram (must be updated via `.env` file on disk). | **Low**.<br>Read-only configuration view is deliberate and safe. |
| **9** | **Pricing Plans & Minute-Only Alignment** | **Handler:** `app/handlers/admin.py` (`handle_admin_pricing`), `app/handlers/payment.py`<br>**Services:** `app/services/payments.py` (`DEFAULT_PRICING_PLANS`, `get_active_pricing_plans`)<br>**Models:** `PricingPlan` | `is_authorized_admin` on `admin:pricing`. | `tests/unit/test_payments.py`<br>`tests/unit/test_admin_handlers.py` (`test_admin_pricing_handler_authorized`) | `PricingPlan` defines one-time minute packages (`MIN_10`: 10m/2000K, `MIN_30`: 30m/5000K, `MIN_60`: 60m/9000K). When purchased, credits `purchased_seconds` directly. No recurring billing, no subscriptions.<br>*Terminology Conflict:* The words "Plan", "Pricing Plans", and 3 occurrences of "tier" in `locales/` cause conceptual confusion with subscription SaaS models. | **Low-Moderate Terminology Confusion**.<br>The underlying mechanism is strictly one-time minute purchases, but terminology needs cleanup. |

---

## 3. Minute-Only Product Conflicts & Obsolete Workflows

### 3.1 Legacy Codebase Comparison
Inspection of the legacy repository (`C:\Users\awint\VOICE_CLONE_PRO_Production_Final`) revealed several obsolete architectures that were deliberately excluded from `VoiceCloneProOfficial`:
- **`plan_entitlements` and `plan_entitlement_changes` (Migration 007 / Phase E):** Complex staged entitlement scheduling for subscription plans.
- **`coupons` (Migration 010):** Promotion coupon redemptions for tier upgrades.
- **`app/services/plan_scheduling.py` / `app/bot/handlers/plan.py`:** Handlers for subscription lifecycle and scheduling.

**Finding:** None of these obsolete subscription tables or services exist in `VoiceCloneProOfficial`. The official schema contains only:
- `users.account_category` (`FREE` vs `PURCHASED`).
- `minute_balances` (`welcome_seconds`, `referral_seconds`, `purchased_seconds`, `consumed_seconds`).
- `minute_ledger_events` (`WELCOME`, `REFERRAL`, `PURCHASE`, `ADJUST`, `CONSUME`).
- `pricing_plans` (minute purchase catalog).

### 3.2 Product Concept Clarification
- **What is being sold:** One-time packs of minutes (e.g., 10 minutes, 30 minutes, 60 minutes).
- **What account status means:**
  - `FREE`: Balance consists only of welcome or referral seconds. Character limit is `character_limit_free` (default 500 characters).
  - `PURCHASED`: User has an active purchased minute balance (`purchased_seconds > 0`). Character limit is `character_limit_purchased` (default 2,000 characters).
  - Model access is identical for both `FREE` and `PURCHASED` accounts (all active system models are accessible).
- **Copy Inconsistencies Found:**
  - `locales/en/messages.json:41`: `"Your account tier allows up to {max_chars} characters."` -> Should be `"Your account type..."`.
  - `locales/en/messages.json:92`: `"User-tier character & minute limits: EXEMPT"` -> Should be `"User account character & minute limits..."`.
  - `locales/en/messages.json:103`: `"Account Tier: PURCHASED"` -> Should be `"Account Status: PURCHASED"`.
  - Admin button `admin_btn_view_pricing`: `"View Pricing Plans"` -> Should be `"View Minute Packs"` (`admin_pricing_title`: `"Active Minute Packages"`).

---

## 4. Security, Authorization, Idempotency & Data Integrity

### 4.1 Authorization Boundary
- Server-side validation via `settings.is_admin(user_id)` is rigorously enforced on every admin command and callback query.
- When an unauthorized user triggers an admin callback, the event answers with alert `"⛔ Access denied. Admin only."` and terminates immediately without exposing data or running queries.

### 4.2 Idempotency Analysis
1. **Payment Approval (`approve_payment_request`):**
   - Utilizes `select(PaymentRequest).with_for_update()` row-level locking.
   - Strictly verifies `payment.status == PaymentStatus.PENDING.value`.
   - Subsequent calls return `(False, payment, "ALREADY_APPROVED")` and never double-credit.
2. **Payment Rejection (`reject_payment_request`):**
   - Verified row-locked; once rejected, cannot be re-approved.
3. **Manual Balance Adjustments (`admin_adjust_balance`):**
   - **Identified Risk:** `admin_adjust_balance` currently does **not** take or record an idempotency key (`reference_id`). If an admin submits an adjustment twice or experiences Telegram network retries, duplicate credits/debits could be applied.
   - **Required Hardening:** Pass a unique `idempotency_token` (e.g. `ADJ-<uuid>`) into `MinuteLedgerEvent.reference_id` and check for its prior existence before applying balance mutations.
4. **Welcome Bonus & Referrals:**
   - Welcome bonus is protected by `WelcomeBonusAward` primary-key constraint and user flag.
   - Referrals are capped at 20 rewards (`ReferralReward` unique constraint on `(referrer_id, reward_index)`).

### 4.3 Data Integrity & HTML Safety
- Error strings from failed jobs and model paths are escaped using `html.escape()` before being inserted into HTML-formatted Telegram messages, preventing Telegram HTML parser crashes (`TelegramBadRequest: can't parse entities`).

---

## 5. Prioritized Recommendations with Exact File References

### Priority 1: User Lookup & Minute Ledger Viewer (High Operational Value)
- **Problem:** Admin cannot inspect an individual user's account category, minute balance breakdown, or ledger events.
- **Files to Modify:**
  - `app/handlers/admin.py`: Add `admin:users_page:<page>` (paginated list of users) and `admin:user:<user_id>` (detailed user card).
  - `app/ui/keyboards.py`: Add paginated keyboard helper `admin_users_list_kb` and `admin_user_card_kb`.
  - `locales/en/messages.json` & `locales/my/messages.json`: Add localized strings for user search and card view.

### Priority 2: Safe Manual Minute Adjustment Workflow (High Operational Value)
- **Problem:** `admin_adjust_balance` exists in `app/services/ledger.py` but is completely unexposed in Telegram and lacks idempotency protection.
- **Files to Modify:**
  - `app/services/ledger.py`: Add `idempotency_key: str | None = None` to `admin_adjust_balance` to guarantee idempotent execution.
  - `app/handlers/admin.py`: Add FSM dialog (`AdminAdjustFSM`) triggered from user card (`admin:user_adjust:<user_id>`), prompting for minute amount and mandatory reason, displaying confirmation preview with nonce, and applying adjustment.
  - `tests/unit/test_ledger.py` & `tests/unit/test_admin_handlers.py`: Add comprehensive unit tests verifying credit, debit, status promotion/demotion, audit logging, and duplicate suppression.

### Priority 3: Interactive Payment Rejection Reason & Receipt Re-Inspection
- **Problem:** Rejecting a payment immediately commits a hardcoded reason without operator input.
- **Files to Modify:**
  - `app/handlers/admin.py`: Introduce an FSM state for `admin:pay_reject:<id>` prompting the admin to enter the rejection reason, or offering preset reasons (`Slip illegible`, `Amount mismatch`, `Duplicate transaction`).
  - Send the original `receipt_file_id` (photo/document) when displaying `admin:pay_detail:<id>`.

### Priority 4: Stale Job Recovery Action on Queue Monitor
- **Problem:** Admin can only view queue counts; cannot trigger recovery for stuck running jobs.
- **Files to Modify:**
  - `app/handlers/admin.py`: Add `admin:queue_recover` callback calling `recover_stale_running_jobs(session, bot=callback.bot)`.
  - `app/ui/keyboards.py`: Add `[🔄 Recover Stale Jobs]` button to queue monitor keyboard.

### Priority 5: Minute-Only Terminology Cleanup
- **Problem:** "Pricing Plans" and "account tier" create confusion with SaaS subscription models.
- **Files to Modify:**
  - `locales/en/messages.json` & `locales/my/messages.json`: Replace "tier" with "account type" and "Pricing Plans" with "Minute Packages".
  - `app/handlers/admin.py`: Rename display labels from `Active Pricing Plans` to `Active Minute Packages`.

---

## 6. Proposed Next Implementation Phase: Phase D14-A Scope

To ensure stability, safety, and focused testing, we recommend executing a tightly scoped **Phase D14-A**:

### Scope of Phase D14-A
1. **User Lookup & Detail Card:**
   - In `admin:users`, add paginated list of registered users (5 per page).
   - Clicking a user opens `admin:user:<user_id>` displaying:
     - User ID, Username, Account Category (`FREE` / `PURCHASED`), Status (`ACTIVE` / `BLOCKED`).
     - Balances: Purchased, Welcome, Referral, Consumed minutes.
     - Last 3 Ledger events.
2. **Safe Manual Minute Adjustment:**
   - On the user card, add `[➕ Adjust Minutes]`.
   - FSM workflow: Select direction (`CREDIT` / `DEBIT`), input minutes, input mandatory reason.
   - Preview card with confirmation token.
   - Hardened `admin_adjust_balance` with idempotency key, ledger event, and audit log.
3. **Queue Stale Recovery Button:**
   - In `admin:queue`, add `[🔄 Recover Stale Jobs]` button invoking `recover_stale_running_jobs`.
4. **Terminology Alignment:**
   - Replace "tier" and "Pricing Plan" strings in localization files with "Minute Packages" and "Account Status".

### Acceptance Tests for Phase D14-A
1. `test_admin_user_card_view`: Verifies user metadata and minute breakdown are rendered accurately with proper HTML escaping.
2. `test_admin_adjust_balance_credit_idempotent`: Verifies crediting minutes increases purchased seconds, promotes account to `PURCHASED`, creates `MinuteLedgerEvent(source=ADJUST)`, logs `AuditLog`, and suppresses duplicate token replays.
3. `test_admin_adjust_balance_debit_to_free`: Verifies debiting purchased seconds to zero demotes account back to `FREE`.
4. `test_admin_adjust_balance_requires_reason`: Rejects adjustments with empty or whitespace-only reason.
5. `test_admin_queue_recover_trigger`: Verifies admin can invoke stale job recovery from callback and receives summary alert.

---

## 7. Safety & Compliance Confirmation

- **Read-Only Verification:** This audit was conducted strictly in read-only mode.
- **Source Code Integrity:** No production application files were created, edited, staged, or deleted.
- **Database Safety:** The production SQLite database (`data/voice_clone_official.db`) was not modified, migrated, or connected to with write transactions (`PRAGMA integrity_check = ok`).
- **Process State:** Zero Python processes or bots are running.
- **Production Bot & External Services:** Telegram APIs were not contacted; no audio inference was executed; no broadcasts were sent.
- **Legacy Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` was inspected read-only and remains completely untouched.
- **Credential Safety:** Zero tokens, passwords, or private user data are contained in this report.
