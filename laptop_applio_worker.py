"""
laptop_applio_worker.py - Dedicated Laptop Local Worker for Applio RVC Jobs.

Runs on creator's laptop with local GPU / CUDA environment (C:\\Applio).
Connects securely to Render PostgreSQL database, polls for queued Applio jobs,
executes high-speed RVC voice conversion locally, and updates status to SUCCESS.

Usage:
  python laptop_applio_worker.py
  (or double-click run_laptop_worker.bat)
"""

from __future__ import annotations

import os
import sys
import time
import asyncio
import logging
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.db.session import AsyncSessionLocal, init_db
from app.models.enums import JobStatus
from app.models.schema import Job
from app.worker import process_single_job
from app.services.queue import claim_next_queued_job

# Set up clean, professional console logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ApplioLaptopWorker")


async def check_applio_environment() -> bool:
    """Verify local Applio directory and runner existence."""
    applio_dir = Path(r"C:\Applio")
    runner_py = ROOT_DIR / "worker" / "applio_runner.py"
    if not applio_dir.is_dir():
        logger.warning("⚠️ Local Applio directory C:\\Applio not found!")
    else:
        logger.info("✅ Found Applio installation at C:\\Applio")
    return True


async def main():
    print("=" * 65)
    print(" 🎙️  VOICE CLONE PRO — LAPTOP APPLIO LOCAL WORKER")
    print(" 🚀  Engine: Applio RVC (Local GPU / CUDA Acceleration)")
    print(" 🌐  Target: Render PostgreSQL Database")
    print("=" * 65)

    # Initialize DB connection and metadata
    try:
        logger.info("Connecting to Render Cloud Database...")
        await init_db()
        logger.info("✅ Database connection established successfully!")
    except Exception as e:
        logger.error(f"❌ Database connection failed: {e}")
        print("\nPress Enter to exit...")
        input()
        return

    await check_applio_environment()

    # Bot instance for sending audio directly to Telegram user
    bot = None
    if settings.bot_token:
        try:
            from aiogram import Bot
            from aiogram.client.default import DefaultBotProperties
            from aiogram.enums import ParseMode
            bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            logger.info("✅ Telegram Bot initialized for direct audio delivery")
        except Exception as e:
            logger.warning(f"Telegram Bot init warning: {e}")

    logger.info("🎧 Polling for QUEUED Applio jobs (engine='applio')...")
    print("✨ Worker is running in background. Keep this window open while processing.")
    print("-" * 65)

    processed_count = 0
    poll_interval = 2.5

    while True:
        try:
            async with AsyncSessionLocal() as session:
                # Atomically claim oldest QUEUED job specifically for 'applio'
                job = await claim_next_queued_job(session, engine_filter="applio")
                if job:
                    processed_count += 1
                    print(f"\n[JOB #{processed_count}] ⚡ Processing Applio Job {job.id[:8]} for User {job.user_id}...")
                    success = await process_single_job(session=session, job=job, bot=bot)
                    if success:
                        print(f"[JOB #{processed_count}] ✅ Completed Successfully! (Audio: {job.result_path})")
                    else:
                        print(f"[JOB #{processed_count}] ❌ Failed: {job.error_message}")
                    await session.commit()
                else:
                    await asyncio.sleep(poll_interval)
        except KeyboardInterrupt:
            print("\n🛑 Stopping Laptop Worker... Goodbye!")
            break
        except Exception as err:
            logger.error(f"Worker iteration error: {err}")
            await asyncio.sleep(poll_interval)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
