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

    if mode in ("all", "bot", "web_and_bot", "bot_and_web"):
        if not bot:
            logger.error("Cannot run bot mode without TELEGRAM_BOT_TOKEN.")
            if mode in ("bot", "web_and_bot", "bot_and_web"):
                return
        else:
            logger.info("Registering Telegram bot commands menu...")
            await setup_bot_commands(bot)
            dp = Dispatcher()
            setup_handlers(dp)
            logger.info("Starting Telegram bot polling...")
            bot_task = asyncio.create_task(dp.start_polling(bot))
            tasks.append(bot_task)

    # Start FastAPI Telegram Mini App & API Web Server
    if mode in ("all", "web", "all_with_web", "web_and_bot", "bot_and_web"):
        try:
            import os
            import uvicorn
            from app.webapp.server import webapp_app

            port = int(os.environ.get("PORT", "8000"))
            config = uvicorn.Config(
                app=webapp_app,
                host="0.0.0.0",
                port=port,
                log_level="info",
                access_log=False,
            )
            server = uvicorn.Server(config)
            logger.info("Starting FastAPI Telegram Mini App web server on 0.0.0.0:%d...", port)
            web_task = asyncio.create_task(server.serve())
            tasks.append(web_task)
        except Exception as e_web:
            logger.warning("Could not launch web server task: %s", e_web)

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
    import os
    env_mode = (os.getenv("RUN_MODE") or os.getenv("APP_MODE") or "").strip().lower()
    default_mode = env_mode if env_mode else "all"

    parser = argparse.ArgumentParser(description="Voice Clone Pro Standalone Telegram Bot & Worker")
    parser.add_argument(
        "--mode",
        choices=["all", "bot", "worker", "web", "web_and_bot", "bot_and_web"],
        default=default_mode,
        help="Service execution mode: 'all', 'bot', 'worker', 'web', or 'web_and_bot'",
    )
    args = parser.parse_args()
    selected_mode = (os.getenv("RUN_MODE") or os.getenv("APP_MODE") or args.mode).strip().lower()
    logger.info("Starting Voice Clone Pro in execution mode: '%s'", selected_mode)

    try:
        asyncio.run(run_bot_and_worker(mode=selected_mode))
    except KeyboardInterrupt:
        logger.info("Process interrupted by user.")


if __name__ == "__main__":
    main()
