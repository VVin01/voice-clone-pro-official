# Phase D16-D — Dependency & Setup Readiness Evidence Report

**Workspace:** `C:\Users\awint\VoiceCloneProOfficial`  
**Legacy Workspace:** `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` *(Strictly Read-Only — Untouched)*  
**Date:** 2026-10-13  
**Auditor:** Independent Senior Python/SQLAlchemy/SQLite Engineer & Release Safety Reviewer  

---

## 1. Final Verdict

### **SETUP READY WITH LIMITATIONS**

The repository now includes an evidence-based, minimal [`requirements.txt`](file:///C:/Users/awint/VoiceCloneProOfficial/requirements.txt) and a comprehensive operator [`README.md`](file:///C:/Users/awint/VoiceCloneProOfficial/README.md).
- **Direct Dependency Selection**: Direct runtime and test dependencies were determined via AST import analysis across all Python source files and matched against installed package metadata. Unrelated globally installed packages were excluded.
- **Dependency Compatibility**: Verified via `pip install --dry-run -r requirements.txt`. All 9 direct packages and their respective transitive dependencies resolve with 100% mutual compatibility.
- **Operator Runbook ([`README.md`](file:///C:/Users/awint/VoiceCloneProOfficial/README.md))**: Documents virtual environment setup, `.env` configuration without secrets, SQLite online WAL backup precautions, testing on isolated databases, launch modes, and crash recovery.
- **Documented Limitation**: A fresh virtual environment creation and full package re-download from PyPI on a separate clean machine was not executed live to avoid network/host modifications. Compatibility was validated via local environment package metadata and pip dry-run resolution.

---

## 2. Dependency Selection Rationale & Pinned Versions

AST parsing of all Python source files in `app/`, `worker/`, `main.py`, and `tests/` identified the following direct third-party packages:

| Package | Pinned Version | Category | Functional Purpose |
| :--- | :---: | :---: | :--- |
| `aiogram` | `3.31.0` | Runtime | Asynchronous Telegram Bot API framework (Aiogram 3.x). |
| `sqlalchemy` | `2.0.52` | Runtime | Database ORM, DeclarativeBase models, and session management. |
| `aiosqlite` | `0.22.1` | Runtime | Asynchronous SQLite driver for SQLAlchemy async engine. |
| `greenlet` | `3.5.5` | Runtime | Required C-extension for SQLAlchemy asyncio context switching. |
| `pydantic` | `2.13.5` | Runtime | Data parsing, validation, and domain models. |
| `pydantic-settings` | `2.15.0` | Runtime | Environment variables loader and `.env` parsing. |
| `edge-tts` | `7.2.8` | Runtime | Microsoft Edge TTS voice synthesis for Burmese & English audio. |
| `pytest` | `9.1.1` | Test | Unit, concurrency, and regression test runner. |
| `pytest-asyncio` | `1.4.0` | Test | Async test fixtures and event loop orchestration. |

*(Note: `worker/applio_runner.py` imports `rvc.infer.infer.VoiceConverter`, which runs within the separate Applio runtime environment located at `APPLIO_WORKDIR` (`C:\Applio`)).*

---

## 3. Files Created

### 3.1. [`requirements.txt`](file:///C:/Users/awint/VoiceCloneProOfficial/requirements.txt)
Created with pinned versions for the 7 runtime and 2 test dependencies. Pinned strictly to known-compatible versions verified in the host environment.

### 3.2. [`README.md`](file:///C:/Users/awint/VoiceCloneProOfficial/README.md)
Concise operator runbook covering:
1. Python prerequisites (Python 3.10.x, Windows x64, Applio).
2. Virtual environment setup (`python -m venv .venv`, `pip install -r requirements.txt`).
3. Configuration setup (`Copy-Item .env.example .env`) without exposing secrets.
4. Database initialization (`init_db()`) and online WAL backup runbook.
5. Isolated test execution (`pytest -v`).
6. Application launch modes (`--mode all`, `--mode bot`, `--mode worker`), with production launch designated as an operator action.
7. Graceful shutdown (`Ctrl+C`) and host reboot crash recovery behavior.

---

## 4. Verification & Isolated Test Results

### 4.1. Pip Dry-Run Dependency Resolution
```powershell
python -m pip install --dry-run -r requirements.txt
```
**Output:** All requirements and transitive dependencies (`aiohttp`, `yarl`, `multidict`, `pydantic-core`, `annotated-types`, etc.) reported satisfied and mutually compatible.

### 4.2. Isolated Test Execution
```powershell
python -m pytest tests/unit/test_admin_idempotency_fix.py tests/unit/test_bot_commands.py -v
```
**Output:** **10 / 10 PASSED** in 6.79s on isolated in-memory/temp-file SQLite databases.

### 4.3. Code Hygiene & Path Audit
- Both `requirements.txt` and `README.md` were inspected:
  - Zero private credentials, tokens, or admin Telegram IDs.
  - Zero machine-specific user paths (`C:\Users\awint\...`).
  - Standard reference path `C:\Applio` aligns with default `APPLIO_WORKDIR`.

---

## 5. Strict Safety Confirmation

During this Phase D16-D:
- **Production Database**: [`data/voice_clone_official.db`](file:///C:/Users/awint/VoiceCloneProOfficial/data/voice_clone_official.db) was **not accessed or modified**.
- **Telegram Connectivity**: **0** Telegram bot API calls made; bot and worker remained stopped.
- **Audio Generation**: **0** TTS or voice conversion tasks executed.
- **Legacy Workspace**: `C:\Users\awint\VOICE_CLONE_PRO_Production_Final` remained strictly read-only and untouched.
- **Git State**: Zero Git commands (`git add`, `git commit`, `git tag`, `git reset`, `git clean`) executed.

---

## 6. Updated Release Scope (54 Files Total)

With the addition of `requirements.txt` and `README.md`, the minimal release scope now consists of **54 files**:

```powershell
# 1. Configuration & Dependency Manifests (3 files)
.gitignore
.env.example
requirements.txt

# 2. Operator Documentation (1 file)
README.md

# 3. Database Schema Migration Artifact (1 file)
migrations/013_adjustment_idempotency.sql

# 4. Core Application & Worker Source (28 files)
main.py
worker/applio_runner.py
app/worker.py
app/config/__init__.py
app/config/settings.py
app/db/base.py
app/db/session.py
app/handlers/__init__.py
app/handlers/admin.py
app/handlers/clone.py
app/handlers/menu.py
app/handlers/payment.py
app/handlers/start.py
app/models/__init__.py
app/models/enums.py
app/models/schema.py
app/services/audio.py
app/services/broadcast.py
app/services/gate.py
app/services/i18n.py
app/services/ledger.py
app/services/models.py
app/services/payments.py
app/services/progress.py
app/services/queue.py
app/services/recovery.py
app/ui/commands.py
app/ui/keyboards.py

# 5. Localization Dictionaries (2 files)
locales/en/messages.json
locales/my/messages.json

# 6. Test Suite (19 files)
tests/conftest.py
tests/unit/test_admin_handlers.py
tests/unit/test_admin_idempotency_fix.py
tests/unit/test_admin_unlimited_access.py
tests/unit/test_admin_user_management.py
tests/unit/test_applio_settings_regression.py
tests/unit/test_audio_and_worker.py
tests/unit/test_bot_commands.py
tests/unit/test_broadcast.py
tests/unit/test_concurrency.py
tests/unit/test_crash_recovery.py
tests/unit/test_i18n.py
tests/unit/test_ledger.py
tests/unit/test_model_catalog_and_scan.py
tests/unit/test_payments.py
tests/unit/test_progress_ui.py
tests/unit/test_queue_concurrency.py
tests/unit/test_safe_model_activation.py
tests/unit/test_telegram_ux_and_auth.py
```
*(All 23 internal phase reports `phase_d*.md` remain untracked locally).*
