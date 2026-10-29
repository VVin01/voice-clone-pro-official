# Voice Clone Pro Official

Production-grade Telegram Voice Studio and Voice Cloning platform designed for high-concurrency audio generation, durable minute ledger accounting, and administrative control.

---

## 1. Prerequisites

- **Operating System:** Windows 10/11 or Windows Server (x64)
- **Python Version:** Python 3.10.x (Recommended: 3.10.11)
- **Voice Engine (Voice v2):** VoxCPM2 only (Hugging Face Spaces, free tokens rotation). Legacy trained-model engine removed; clone with a short reference audio instead.

---

## 2. Environment Setup

### 2.1. Create and Activate Virtual Environment
Open PowerShell in the project directory:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 2.2. Install Dependencies
Install direct dependencies from `requirements.txt`:

```powershell
pip install -r requirements.txt
```

---

## 3. Configuration Setup

Copy the template configuration file to `.env`:

```powershell
Copy-Item .env.example .env
```

Open `.env` and fill in the required variables (do **not** commit `.env` to Git):
- `TELEGRAM_BOT_TOKEN`: Your Telegram Bot API token from [@BotFather](https://t.me/BotFather).
- `ADMIN_IDS`: Comma-separated Telegram User IDs for authorized administrators.
- `REQUIRED_CHANNEL_ID` / `REQUIRED_GROUP_ID`: Telegram chat IDs for membership gate enforcement.
- `DATABASE_URL`: code default is `sqlite+aiosqlite:///./data/voice_clone_v2.db`. On Render, confirm whether the live service uses Render Postgres or local SQLite - a local file is lost on redeploy without a persistent disk.
- `WORKER_TOKEN`: a random value, set to the same value on Render and on any laptop worker (security branch). If empty, the bot token is used as a fallback with a warning.
- `CHARACTER_LIMIT_FREE` / `CHARACTER_LIMIT_PURCHASED`: code defaults are 1000 / 5000 (the old .env.example values 120 / 500 were outdated).
- Voice v2: legacy trained-model env vars removed. See `VOXCPM_SPACE_URL`, `VOXCPM_SPACES`, `VOXCPM_TIMEOUT`, `HF_TOKENS`.

---

## 4. Database Initialization & Safety Precautions

### 4.1. Automatic Table Provisioning
The application automatically provisions all required database tables (including `adjustment_idempotency_records`) at service startup via `init_db()`. No manual SQL migrations are required.

### 4.2. Recommended Backup Procedure (WAL-Safe)
Before making production changes or updates, create an atomic SQLite online backup using Python:

```powershell
python -c "import sqlite3, datetime; ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S'); src = sqlite3.connect('file:data/voice_clone_official.db?mode=ro', uri=True); dst = sqlite3.connect(f'data/backups/voice_clone_official_{ts}.db'); src.backup(dst); dst.close(); src.close(); print('Backup complete.')"
```

---

## 5. Running the Test Suite (Isolated Databases)

All unit, concurrency, crash recovery, and security tests run against isolated in-memory or temporary file-backed SQLite databases. Tests **never** touch the production database:

```powershell
pytest -v
```

Run the full test suite and do not deploy if failures remain unexplained. The old "132 tests" claim is outdated: the suite now contains far more tests (255 were found in the 2026-10-28 branch-vs-main comparison), and main itself had pre-existing failures, so treat the count as a check, not a guarantee.

---

## 6. Service Execution Modes

> [!IMPORTANT]
> **Operator Action Required**: Launching the application in production connects directly to Telegram and claims queue items. Ensure `.env` is configured correctly before starting.

### Mode 1: Combined Service (Bot Polling + Background Audio Worker)
Runs both the Telegram bot dispatcher and the audio worker loop in a single process:

```powershell
python main.py --mode all
```

### Mode 2: Standalone Telegram Bot Only
Runs only the Telegram bot polling and administrative interface (skips audio synthesis worker):

```powershell
python main.py --mode bot
```

### Mode 3: Standalone Audio Worker Only
Runs only the background audio processing queue consumer (skips Telegram bot polling):

```powershell
python main.py --mode worker
```

### Mode 4: Web Server Only
Runs only the FastAPI Mini App web server (no bot polling, no worker):

```powershell
python main.py --mode web
```

### Mode 5: Web + Bot (no worker)
Runs the web server and the Telegram bot, without the audio worker loop:

```powershell
python main.py --mode web_and_bot
```

### Mode 6: Bot + Web (alias)
`bot_and_web` is an alias for `web_and_bot`. If `RUN_MODE` or `APP_MODE` is set in the environment, it overrides `--mode`.

> **Live service note:** On Render, the live service is `voice-clone-studio`, a `web_service` in Oregon on the standard plan, with `autoDeploy: true` and a `/health` endpoint in code. The `render.yaml` in this repo previously described a different worker service; it has been updated to match the live service's shape, but it is not automatically the live definition - do not re-apply it blindly.

---

## 7. Windows Process Management & Crash Recovery

### 7.1. Graceful Shutdown
To stop the running service, press `Ctrl+C` in the terminal window. The application intercepts the signal, cancels active asyncio tasks, closes the Telegram bot session, and exits cleanly.

### 7.2. Automatic Startup Crash Recovery
If the host server reboots or crashes unexpectedly while audio synthesis jobs are in `RUNNING` status:
- On the next service startup, `recover_stale_running_jobs` automatically sweeps the queue before processing new tasks.
- Orphaned running jobs older than the configured stale timeout (code default 1800 seconds) are marked as `FAILED`.
- Any debited minutes are automatically and idempotently refunded to the user's ledger balance.

---

## 8. Code Layout Note (polish-premium-2026-10-28)

Large files are documented, not refactored, in this branch: `app/handlers/admin.py`, `app/webapp/api.py`, `app/services/recap_engine/core.py`, `app/services/recap_engine/recap_planner.py`, and the Mini App `static/js/app.js` / `static/index.html` are each over 100 KB. Splitting them is a separate, higher-risk change and was deliberately not done here. See `docs/ARCHITECTURE.md` for the map.

Security fixes from `security-fixes-audit-2026-10-28` are included in this branch's base: guest fallback removed, job ownership checks, Mini App queue validation, separate `WORKER_TOKEN`, gate fail-closed, and Telegram-only CORS. Those fixes are not live until this branch (or a later one) is merged and deployed.
