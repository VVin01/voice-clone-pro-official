"""
laptop_applio_worker.py - Dedicated Laptop Local Worker for Applio RVC & Voice Jobs.

Runs on creator's laptop with local GPU / CUDA environment (C:\\Applio).
Connects securely to Render PostgreSQL database, polls for queued jobs,
pings worker heartbeat so Admin Mini App shows real-time online status,
executes high-speed voice conversion, and updates status to SUCCESS.

Usage:
  python laptop_applio_worker.py --engine all
  python laptop_applio_worker.py --engine applio
  python laptop_applio_worker.py --engine voxcpm
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import time
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Ensure UTF-8 stdio on Windows terminals to prevent charmap encoding errors
for stream_name in ("stdout", "stderr"):
    stream = getattr(sys, stream_name, None)
    if stream and hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from app.config import settings
from app.db.session import AsyncSessionLocal, init_db
from app.models.enums import JobStatus
from app.models.schema import Job, utc_now
from app.services.dynamic_settings import set_dynamic_setting
from app.services.queue import claim_next_queued_job
from app.worker import process_single_job

# Set up clean, professional console logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("VoiceWorker")


async def check_applio_environment() -> bool:
    """Verify local Applio directory and runner existence."""
    applio_dir = Path(r"C:\Applio")
    if not applio_dir.is_dir():
        logger.warning("⚠️ Local Applio directory C:\\Applio not found! (Applio jobs may fail, VoxCPM jobs unaffected)")
        return False
    logger.info("✅ Found Applio installation at C:\\Applio (GPU / CUDA Enabled)")
    return True


async def update_worker_heartbeat(engine_mode: str, is_active: bool = True) -> None:
    """Update worker heartbeat in Cloud database for Admin Panel Live Monitor."""
    try:
        async with AsyncSessionLocal() as session:
            await set_dynamic_setting(session, "worker_laptop_heartbeat", utc_now().isoformat())
            await set_dynamic_setting(session, "worker_laptop_engine", engine_mode)
            await set_dynamic_setting(session, "worker_laptop_status", "ONLINE" if is_active else "OFFLINE")
            await set_dynamic_setting(session, "worker_laptop_host", socket.gethostname())
            await session.commit()
    except Exception as e:
        logger.debug(f"Heartbeat ping warning: {e}")


async def heartbeat_loop(engine_mode: str, stop_event: asyncio.Event) -> None:
    """Background task to continuously pulse worker heartbeat every 10 seconds."""
    while not stop_event.is_set():
        await update_worker_heartbeat(engine_mode, is_active=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            pass
    # Worker is terminating
    await update_worker_heartbeat(engine_mode, is_active=False)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Voice Clone Pro High-Speed Local GPU Worker")
    parser.add_argument(
        "--engine",
        choices=["all", "applio", "voxcpm"],
        default=os.getenv("WORKER_ENGINE", "all").lower(),
        help="Engine to process: 'all' (default, claims both Applio & VoxCPM), 'applio', or 'voxcpm'",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Polling interval in seconds (default: 2.0)",
    )
    args = parser.parse_args()

    selected_engine = args.engine
    engine_filter = None if selected_engine == "all" else selected_engine

    print("=" * 68)
    print(" 🎙️  VOICE CLONE PRO — HIGH-SPEED LOCAL GPU WORKER")
    print(f" 🚀  Engine Mode: {selected_engine.upper()} " + ("(Applio GPU + VoxCPM AI)" if selected_engine == "all" else ""))
    print(" 🌐  Target: Render Cloud PostgreSQL Database")
    print(f" 💻  Host Machine: {socket.gethostname()}")
    print("=" * 68)

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

    if selected_engine in ("all", "applio"):
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

    # Launch background heartbeat updater
    stop_event = asyncio.Event()
    heartbeat_task = asyncio.create_task(heartbeat_loop(selected_engine, stop_event))
    logger.info("💓 Live Heartbeat Monitor initialized (Reporting to Admin Mini App)")

    filter_desc = "ALL engines (Applio & VoxCPM)" if not engine_filter else f"engine='{engine_filter}'"
    logger.info(f"🎧 Polling for QUEUED voice jobs ({filter_desc})...")
    print("✨ Worker is running in background. Keep this window open while processing.")
    print("-" * 68)

    processed_count = 0
    poll_interval = max(0.5, args.interval)

    try:
        while True:
            try:
                async with AsyncSessionLocal() as session:
                    # Atomically claim oldest QUEUED job matching engine_filter
                    job = await claim_next_queued_job(session, engine_filter=engine_filter)
                    if job:
                        processed_count += 1
                        t_start = time.time()
                        engine_badge = (getattr(job, "engine", "") or "VOXCPM").upper()
                        print(f"\n[JOB #{processed_count}] ⚡ Claimed {engine_badge} Job {job.id[:8]} for User {job.user_id}...")
                        print(f"[JOB #{processed_count}] ⏳ Processing voice conversion...")

                        success = await process_single_job(session=session, job=job, bot=bot)
                        duration = time.time() - t_start

                        if success:
                            print(f"[JOB #{processed_count}] ✅ Completed in {duration:.1f}s! (Audio: {job.result_path})")
                        else:
                            print(f"[JOB #{processed_count}] ❌ Failed in {duration:.1f}s: {job.error_message}")
                        await session.commit()
                    else:
                        await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as err:
                logger.error(f"Worker iteration error: {err}")
                await asyncio.sleep(poll_interval)
    finally:
        print("\n🛑 Stopping Local Worker... Setting heartbeat to OFFLINE...")
        stop_event.set()
        try:
            await asyncio.wait_for(heartbeat_task, timeout=3.0)
        except Exception:
            pass
        if bot:
            try:
                await bot.session.close()
            except Exception:
                pass
        print("👋 Local Worker shutdown complete. Goodbye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
