# Voice Clone Pro Official

Production-grade Telegram Voice Studio and Voice Cloning platform designed for high-concurrency audio generation, durable minute ledger accounting, and administrative control.

---

## 1. Prerequisites

- **Operating System:** Windows 10/11 or Windows Server (x64)
- **Python Version:** Python 3.10.x (Recommended: 3.10.11)
- **External Voice Engine (Optional for worker mode):** [Applio RVC](https://github.com/IAHSPCS/Applio) installed at `C:\Applio` (or configured via `APPLIO_WORKDIR`).

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
- `DATABASE_URL`: Defaults to local SQLite WAL database: `sqlite+aiosqlite:///./data/voice_clone_official.db`.
- `APPLIO_WORKDIR`: Local filesystem path to the Applio installation (e.g. `C:\Applio`).

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

All 132 tests should pass before launching or deploying the application.

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

---

## 7. Windows Process Management & Crash Recovery

### 7.1. Graceful Shutdown
To stop the running service, press `Ctrl+C` in the terminal window. The application intercepts the signal, cancels active asyncio tasks, closes the Telegram bot session, and exits cleanly.

### 7.2. Automatic Startup Crash Recovery
If the host server reboots or crashes unexpectedly while audio synthesis jobs are in `RUNNING` status:
- On the next service startup, `recover_stale_running_jobs` automatically sweeps the queue before processing new tasks.
- Orphaned running jobs older than 600 seconds are marked as `FAILED`.
- Any debited minutes are automatically and idempotently refunded to the user's ledger balance.
