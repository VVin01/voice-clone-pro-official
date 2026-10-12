from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import settings
from app.db.session import init_db
from app.handlers import setup_handlers
from app.ui.commands import setup_bot_commands
from app.worker import run_worker_loop

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("voice_clone_official")


async def run_bot_and_worker(mode: str) -> None:
    logger.info("Initializing database...")
    await init_db()

    bot = None
    if settings.bot_token:
        bot = Bot(
            token=settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
    else:
        logger.warning("TELEGRAM_BOT_TOKEN is not set. Bot polling will not start unless provided.")

    tasks = []

    if mode in ("all", "worker"):
        logger.info("Checking for orphaned stale jobs before launching worker...")
        from app.db.session import AsyncSessionLocal
        from app.services.recovery import recover_stale_running_jobs
        try:
            async with AsyncSessionLocal() as session:
                recovered = await recover_stale_running_jobs(session=session, bot=bot)
                if recovered:
                    logger.info("Recovered %d orphaned stale jobs on service boot: %s", len(recovered), recovered)
        except Exception as e:
            logger.warning("Error during boot stale job recovery sweep: %s", e)

        logger.info("Starting background audio worker task...")
        worker_task = asyncio.create_task(run_worker_loop(bot=bot))
        tasks.append(worker_task)

    if mode in ("all", "bot"):
        if not bot:
            logger.error("Cannot run bot mode without TELEGRAM_BOT_TOKEN.")
            if mode == "bot":
                return
        else:
            logger.info("Registering Telegram bot commands menu...")
            await setup_bot_commands(bot)
            dp = Dispatcher()
            setup_handlers(dp)
            logger.info("Starting Telegram bot polling...")
            bot_task = asyncio.create_task(dp.start_polling(bot))
            tasks.append(bot_task)

    if not tasks:
        logger.info("No tasks to run. Exiting.")
        return

    try:
        await asyncio.gather(*tasks)
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Service shutdown requested.")
    finally:
        for t in tasks:
            t.cancel()
        if bot:
            await bot.session.close()
        logger.info("Service shut down cleanly.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Voice Clone Pro Standalone Telegram Bot & Worker")
    parser.add_argument(
        "--mode",
        choices=["all", "bot", "worker"],
        default="all",
        help="Service execution mode: 'all' (bot + worker), 'bot', or 'worker'",
    )
    args = parser.parse_args()

    try:
        asyncio.run(run_bot_and_worker(mode=args.mode))
    except KeyboardInterrupt:
        logger.info("Process interrupted by user.")


if __name__ == "__main__":
    main()
