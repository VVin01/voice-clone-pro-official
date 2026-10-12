-- Migration: 013_adjustment_idempotency.sql
-- Description: Creates the adjustment_idempotency_records table to enforce durable,
-- race-safe idempotency for administrative minute adjustments.
-- Workspace: VoiceCloneProOfficial
-- Date: 2026-10-12
-- Preconditions:
-- 1. Table `users` must already exist.
-- 2. This migration is completely additive and backward-compatible.
-- 3. Does not alter or restrict existing minute_ledger_events rows or job consumption.

CREATE TABLE IF NOT EXISTS adjustment_idempotency_records (
    idempotency_key VARCHAR(64) PRIMARY KEY,
    user_id BIGINT NOT NULL,
    admin_id BIGINT NOT NULL,
    seconds BIGINT NOT NULL,
    direction VARCHAR(20) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_adjustment_idempotency_user_id ON adjustment_idempotency_records(user_id);
