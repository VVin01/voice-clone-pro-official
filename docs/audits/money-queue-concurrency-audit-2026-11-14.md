# Part C Audit: Money Paths, Queue/Park Accounting, Concurrency & Ledger Consistency

**Date:** 2026-11-14  
**Branch:** `audit/money-queue-concurrency-part-c` (off `origin/main` `c924228`)  
**Frozen files respected:** `app/webapp/telegram_webhook.py` (audited read-only, zero edits); no user-visible billing policy changed silently.

---

## Verdict Summary

| Area | Scenario / Surface | Primary Code Paths | Status |
|---|---|---|---|
| **1. Charge / Refund Symmetry** | Recap submit credits + Option B voice hold | `create_recap_job` (`app/webapp/api.py:9465`), `consume_seconds` (`app/services/ledger.py:506`) | `safe` |
| **1. Charge / Refund Symmetry** | Recap segment & duration-band top-ups | `_top_up_recap_charge_to_total` (`app/webapp/api.py:2981`), `_top_up_recap_segment_charge` (`app/webapp/api.py:3041`), `_apply_recap_band_top_up` (`app/webapp/api.py:3073`) | `safe` |
| **1. Charge / Refund Symmetry** | Option B voice settle under-run & overrun | `settle_recap_voice_seconds` (`app/services/ledger.py:786`), caller at `app/webapp/api.py:8486` | **`fixed`** (`506f648`) |
| **1. Charge / Refund Symmetry** | TTS referral draw / settle / failure refund | `draw_tts_referral_seconds` (`app/services/ledger.py:1016`), `refund_tts_referral_seconds` (`app/services/ledger.py:1087`), `app/services/tts_service.py` | **`fixed`** (`33530e1`) |
| **1. Charge / Refund Symmetry** | Worker crash mid-job (voice clone & TTS) | `process_single_job` (`app/worker.py:264`), `recover_stale_running_jobs` (`app/services/recovery.py:23`), `refund_job_seconds` (`app/services/ledger.py:650`) | **`fixed`** (`33530e1` + `safe` recovery claim) |
| **1. Charge / Refund Symmetry** | Deploy restart mid-job & stranded recap cap | `reset_stale_running_recap_jobs` (`app/webapp/server.py:51`), `_fail_stranded_recap_job` (`app/webapp/api.py:2629`) | `safe` |
| **2. `WAITING_CHOICE` Accounting** | Parked job holds charge; excluded from concurrent slot | `apply_user_choice` (`app/services/voice_choice.py:272`), `_recap_active_job_count`, `create_voice_clone_job` (`app/services/queue.py:79`) | `safe` |
| **2. `WAITING_CHOICE` Accounting** | Resume via callback or capacity sweep (no double charge) | `handle_voice_choice_callback` (`app/handlers/clone.py:695`), `sweep_parked_jobs` (`app/services/voice_choice.py:453`), `_recap_redispatch_plan` | `safe` |
| **2. `WAITING_CHOICE` Accounting** | 48 h expiry after resume / concurrent cancel races | `_expire_parked_job` (`app/services/voice_choice.py:338`), `_cancel_recap_job_core` (`app/webapp/api.py:3218`), `_claim_job_cancelled` (`app/webapp/api.py:3182`) | **`fixed`** (`4d8fcac`, `33530e1`) |
| **2. `WAITING_CHOICE` Accounting** | Orphan / retention sweep vs `dp_refs` & `recap_output` | `_sweep_storage_orphans` (`app/webapp/api.py:9089`), `cleanup_expired_jobs` (`app/webapp/api.py:9286`) | `safe` |
| **3. Concurrency** | Simultaneous submits for same user (recap/recap, voice/voice, cross-type) | `create_recap_job` (`User` + `MinuteBalance` `FOR UPDATE`), `create_voice_clone_job` (`User` `FOR UPDATE`) | `safe` |
| **3. Concurrency** | Cancel while `RUNNING` / `SCRIPT_READY` / `WAITING_CHOICE` & cancel vs completion | `_claim_job_cancelled` (`app/webapp/api.py:3182`), `_cancel_recap_job_core`, `cancel_voice_job` (`app/webapp/api.py:4907`), `cancel_queued_job` (`app/webapp/api.py:13622`), `expire_abandoned_script_ready_jobs` (`app/webapp/api.py:2444`) | **`fixed`** (`4d8fcac`) |
| **3. Concurrency** | Double admin queue retry & `User.recap_credits` RMW | `retry_queued_job` (`app/webapp/api.py:13443`) | **`fixed`** (`4d8fcac`) |
| **3. Concurrency** | Duplicate webhook deliveries | `RecentUpdateIds` (`app/webapp/telegram_webhook.py:165`), DB guards (`uq_pr_slip_fingerprint_live`, `WHERE status == WAITING_CHOICE`) | `safe` *(frozen read-only; see note)* |
| **4. Ledger Consistency** | `MinuteBalance` derivable from `MinuteLedgerEvent` (`CREDIT - DEBIT`) | `app/services/ledger.py` (`consume_seconds`, `refund_job_seconds`, `settle_recap_voice_seconds`, `admin_adjust_balance`) | `safe` *(`ADJUST` tags bucket in `note`)* |
| **4. Ledger Consistency** | `consumed_seconds` display counter on `refund_job_seconds` vs `settle_recap_voice_seconds` | `app/services/ledger.py:636, 650-740, 920-935`, `app/handlers/menu.py:427,567` | **`needs-Ulfa-decision`** |
| **4. Ledger Consistency** | `RecapCreditLedgerEvent` coverage on payment approval & signup grants | `approve_payment_request` (`app/services/payments.py:369`), `get_or_create_user` | **`needs-Ulfa-decision`** |

---

## Fixed Non-Policy Bugs

1. **`33530e1` — Missing `User` row locks in `refund_job_seconds` and `refund_tts_referral_seconds` (`app/services/ledger.py:650`, `1087`)**
   - **Root cause:** Unlike `credit_purchased_minutes` (`465`), `consume_seconds` (`506`), `settle_recap_voice_seconds` (`786`), `grant_monthly_free_allowance`, and `admin_adjust_balance` (`1251`), neither `refund_job_seconds` nor `refund_tts_referral_seconds` acquired `SELECT ... FROM users WHERE id = :user_id FOR UPDATE`. Because `minute_ledger_events` has no partial unique constraint on `REFUND:<job_id>`, concurrent refund calls under Postgres `READ COMMITTED` could both read `prior_refunds == []` and double-credit `MinuteBalance`.
   - **Fix:** Acquire `select(User).where(User.id == user_id).with_for_update()` at entry to both `refund_job_seconds` and `refund_tts_referral_seconds` before reading prior refunds/debits/net draw.
   - **Verification:** `tests/unit/test_queue_concurrency.py` (`test_refund_functions_lock_user_row_with_for_update`, `test_concurrent_refund_job_seconds_and_tts_referral_never_double_credit`).

2. **`506f648` — `settle_recap_voice_seconds` overrun `note` dropped the overrun prefix (`app/services/ledger.py:972-1002`)**
   - **Root cause:** `settle_recap_voice_seconds` identifies prior overrun debits by checking `(r.note or "").startswith(f"Recap clone settle overrun ({job_id})")`. However `app/webapp/api.py:8491` passes `note=f"Recap clone settle ({job_id})"`, and the overrun branch previously wrote `note=note or f"{overrun_prefix} (...)"`, omitting `overrun_prefix`. On replay, `prior_overrun_charged` computed `0`, inflating `held_seconds` and bypassing overrun idempotency (notably charging again if a capped overrun was followed by a balance top-up and settle replay).
   - **Fix:** Consolidated the three overrun bucket debits into a loop that always starts `MinuteLedgerEvent.note` with `overrun_prefix`, appending caller `note` as a suffix when provided.
   - **Verification:** `tests/unit/test_phase_5_recap_billing_and_ui_vcp.py` (`test_p5_overrun_with_caller_note_remains_idempotent_on_replay`).

3. **`4d8fcac` — Unconditional `Job.status` writes and unlocked retry RMW on cancel & admin retry (`app/webapp/api.py:3182`, `3218`, `4907`, `13443`, `13622`)**
   - **Root cause:**
     - `cancel_voice_job`, `cancel_queued_job`, and `_cancel_recap_job_core` checked `job.status` in Python then executed an unguarded ORM attribute write `job.status = CANCELLED; await db.commit()`, allowing double cancels or a cancel holding a stale `RUNNING` instance to overwrite a just-committed `SUCCESS` job and trigger a refund for delivered output.
     - `retry_queued_job` loaded `User` without `with_for_update()`, mutated `recap_credits` via ORM RMW, and updated `Job.status = QUEUED` unconditionally, allowing concurrent admin retries to debit twice or spawn duplicate background recap tasks.
   - **Fix:**
     - Added shared `_claim_job_cancelled` helper (`UPDATE jobs SET status='CANCELLED' ... WHERE id = :id AND status IN ('QUEUED', 'RUNNING', 'WAITING_CHOICE')`) used across `_cancel_recap_job_core`, `cancel_voice_job`, and `cancel_queued_job`, rolling back and raising `409`/`400` when `rowcount == 0`.
     - Locked `User` `WITH FOR UPDATE` in `retry_queued_job`, atomically guarded the `User.recap_credits >= _debit_total` decrement, and atomically guarded `UPDATE jobs SET status='QUEUED' ... WHERE id = :id AND status IN ('FAILED', 'CANCELLED')` (rolling back staged debits if `rowcount == 0`).
   - **Verification:** `tests/unit/test_batch_aj_api.py` (`test_part_c_concurrent_admin_retry_charges_and_dispatches_once`, `test_part_c_concurrent_cancels_and_cancel_after_success_race`) and `tests/unit/test_decision_batch_n_2026_11_04.py`.

---

## Items Flagged `needs-Ulfa-decision` (Not Modified)

1. **`RecapCreditLedgerEvent` does not emit `CREDIT` rows on payment approval or initial signup grant**
   - `approve_payment_request` (`app/services/payments.py:440-447`) atomically increments `User.recap_credits` and updates `account_category`, unlike `credit_purchased_minutes` (`app/services/ledger.py:465`), which writes `MinuteLedgerEvent(PURCHASE, CREDIT)`. Likewise signup default `recap_credits = 3` emits no `RecapCreditLedgerEvent`.
   - As a result, `SUM(CREDIT) - SUM(DEBIT)` on `RecapCreditLedgerEvent` reconciles delta from signup/purchased grants rather than absolute zero-based balance, and pre-Batch-O users have no baseline `CREDIT` rows. Emitting `PURCHASE`/signup `CREDIT` rows (and deciding historical backfill vs opening-balance policy) changes existing test/ledger assumptions (`test_phase_5_recap_billing_and_ui_vcp.py:220`) — **needs Ulfa decision**.

2. **`MinuteBalance.consumed_seconds` asymmetry between under-run settle and full failure/cancel refund**
   - `consume_seconds` increments `balance.consumed_seconds` (`app/services/ledger.py:636`). On Option B under-run completion, `settle_recap_voice_seconds` decrements `balance.consumed_seconds` (`app/services/ledger.py:932`). On full failure/cancel/crash recovery, `refund_job_seconds` restores every spendable bucket and emits exact `MinuteLedgerEvent(CREDIT)` rows (`app/services/ledger.py:650-740`), but leaves `consumed_seconds` incremented (which is displayed in `/menu` / `/balance` via `app/handlers/menu.py:427,567` and Admin user detail via `app/webapp/api.py:12916`).
   - Spendable balances and `MinuteLedgerEvent` are unaffected; changing whether `consumed_seconds` represents gross billed attempts or net settled usage changes user-visible stats — **needs Ulfa decision**.

---

## Detailed Notes by Audit Area

- **Area 1 (Symmetry):**
  - Recap submit (`create_recap_job`) locks `User` and `MinuteBalance` `FOR UPDATE`, deducts `recap_credits >= 1`, holds Option B `consume_seconds`, inserts `RecapCreditLedgerEvent(DEBIT)` and `Job` (inside nested savepoint), and commits once (`safe`; savepoint failure preserves charge + registry execution by design at `app/webapp/api.py:9920`).
  - Segment/band top-ups commit additional `RecapCreditLedgerEvent(DEBIT)` rows under the same `job_id`; failure/cancel refunds read `recap_debit_total_for_job` + `_claim_persisted_recap_refund` CAS + `uq_recap_ledger_refund_ref` (`safe`).
  - Voice clone jobs bill post-synthesis (`app/worker.py:701`); worker crashes before billing deduct nothing, and stale crashes after billing are reclaimed once via atomic `RUNNING -> FAILED` in `recover_stale_running_jobs` + `refund_job_seconds` (now locked via `33530e1`).
  - Deploy restarts reset stale `RUNNING` recap jobs via `reset_stale_running_recap_jobs` with `restore_credit_state=True` (no second charge) up to `RECAP_STRANDED_RESET_LIMIT`, then fail and refund via `_fail_stranded_recap_job` (`safe`; verified in `8558ca6`).

- **Area 2 (`WAITING_CHOICE`):**
  - Parked jobs keep submit charges and are excluded from concurrent-job slot counts (`test_batch_dp_2026_11_11.py:1052`, owner design).
  - Resume transitions (`apply_user_choice`, `sweep_parked_jobs`, `_expire_parked_job`) all guard on `WHERE status = 'WAITING_CHOICE'`, and cancel transitions now guard on `WHERE status IN ('QUEUED', 'RUNNING', 'WAITING_CHOICE')` (`4d8fcac`), so callback retry, capacity sweep, expiry after resume, and cancel races never double-charge or double-refund.
  - `_sweep_storage_orphans` (`app/webapp/api.py:9089`) excludes active/parked `recap_output/<job_id>`, skips `dp_refs/` altogether, and preserves `temp/voxcpm_refs/` referenced by surviving `Job.recap_ref_audio_path` rows (`safe`; verified in `8558ca6`).

- **Area 3 (Concurrency & Webhook Dedup):**
  - Simultaneous recap submits lock `User` + `MinuteBalance` `FOR UPDATE`; voice clone submits lock `User` `FOR UPDATE` (`app/services/queue.py`); cross-type voice+recap submits against shared `max_concurrent_jobs_per_user=1` serialize on the `User` row without over-holding (`verified in 8558ca6`).
  - `app/webapp/telegram_webhook.py` was audited read-only and not modified: `RecentUpdateIds` (`maxlen=4096`, FIFO eviction) deduplicates webhook and polling updates within a single process; multi-worker deployments rely on DB-level idempotency (`uq_pr_slip_fingerprint_live`, conditional `WHERE status = 'WAITING_CHOICE'`).

- **Area 4 (Ledger Derivation):**
  - `MinuteBalance.total_usable_seconds == SUM(CREDIT) - SUM(DEBIT)` across `MinuteLedgerEvent` holds identically after every grant, purchase, voice consume/refund, Option B hold/under-run/overrun settle, TTS referral draw/settle, and `admin_adjust_balance` call. Because `admin_adjust_balance` writes `source="ADJUST"` with the bucket tagged in `note` (`({source.value})`), per-bucket reconstruction attributes `ADJUST` rows using that tag (`tests/unit/test_ledger.py::test_part_c_minute_balance_derivable_from_ledger_events`, `8558ca6`).
