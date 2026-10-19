from __future__ import annotations

import asyncio
import html
import logging
import time
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from app.services.i18n import t

logger = logging.getLogger(__name__)


def render_stepper_progress(
    step: int,
    model_name: str,
    elapsed_seconds: int,
    spinner: str = "⏳",
    lang: str = "my",
) -> str:
    """Format an honest, Burmese-first 4-stage stepper message with live elapsed time."""
    safe_model = html.escape(str(model_name or "Standard"))
    mins, secs = divmod(elapsed_seconds, 60)
    elapsed_str = f"{mins:02d}:{secs:02d}"

    title = t("progress_title", lang)
    model_lbl = t("progress_model_label", lang, safe_model=safe_model)
    time_lbl = t("progress_elapsed_time", lang, elapsed_str=elapsed_str)
    steps_def = [
        (
            t("progress_step1_name", lang),
            t("progress_state_done", lang),
            t("progress_step1_active", lang),
            t("progress_state_waiting", lang),
        ),
        (
            t("progress_step2_name", lang),
            t("progress_state_done", lang),
            t("progress_step2_active", lang),
            t("progress_state_waiting", lang),
        ),
        (
            t("progress_step3_name", lang),
            t("progress_state_done", lang),
            t("progress_step3_active", lang, spinner=spinner),
            t("progress_state_waiting", lang),
        ),
        (
            t("progress_step4_name", lang),
            t("progress_state_done", lang),
            t("progress_step4_active", lang),
            t("progress_state_waiting", lang),
        ),
    ]
    footnote = t("progress_footnote", lang)

    progress_bars = {
        1: "▰▰▱▱▱▱▱▱ 25%",
        2: "▰▰▰▰▱▱▱▱ 50%",
        3: "▰▰▰▰▰▰▱▱ 75%",
        4: "▰▰▰▰▰▰▰▰ 100%",
    }
    bar_str = progress_bars.get(step, "▰▰▰▰▰▰▱▱ 75%")
    bar_lbl = f"📊 တိုးတက်မှု: <code>{bar_str}</code>" if lang == "my" else f"📊 Progress: <code>{bar_str}</code>"

    lines = [title, "━━━━━━━━━━━━━━━━━━━━", model_lbl, time_lbl, bar_lbl, ""]
    for i, (name, done_txt, active_txt, wait_txt) in enumerate(steps_def, 1):
        if i < step:
            lines.append(f"✅ <b>[{i}/4] {name}:</b> <i>{done_txt}</i>")
        elif i == step:
            lines.append(f"🔄 <b>[{i}/4] {name}:</b> <b>{active_txt}</b>")
        else:
            lines.append(f"⏳ <b>[{i}/4] {name}:</b> <i>{wait_txt}</i>")

    lines.extend(["━━━━━━━━━━━━━━━━━━━━", footnote])
    return "\n".join(lines)


async def safe_edit_status(
    bot: Bot | None,
    chat_id: int | None,
    message_id: int | None,
    status_text: str,
) -> bool:
    """Update user-facing progress message with edit rate limit protection and flood handling."""
    if not bot or not chat_id or not message_id:
        return False
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=status_text,
            parse_mode="HTML",
        )
        return True
    except TelegramRetryAfter as e:
        logger.warning(
            "Telegram flood limit for %s:%s; waiting %ss",
            chat_id, message_id, e.retry_after,
        )
        await asyncio.sleep(min(float(e.retry_after), 5.0))
        return False
    except TelegramBadRequest as e:
        err_msg = str(e).lower()
        if "message is not modified" in err_msg:
            return True
        logger.debug("Bad request editing status %s:%s: %s", chat_id, message_id, e)
        return False
    except Exception as e:
        logger.debug("Failed to edit status message %s:%s: %s", chat_id, message_id, e)
        return False


class JobProgressTracker:
    """Manages 4-step progress updates and background animation ticker for long-running jobs."""

    # Premium audio waveform equalizer visualizer frames for AI voice synthesis
    SPINNER_FRAMES = [
        "⚡ [ ▂▃▅ ]",
        "⚡ [ ▃▅▆ ]",
        "⚡ [ ▅▆▇ ]",
        "⚡ [ ▆▇█ ]",
        "⚡ [ ▇█▇ ]",
        "⚡ [ █▇▆ ]",
        "⚡ [ ▇▆▅ ]",
        "⚡ [ ▆▅▃ ]",
        "⚡ [ ▅▃▂ ]",
        "⚡ [ ▃▂  ]",
    ]

    def __init__(
        self,
        bot: Bot | None,
        chat_id: int | None,
        message_id: int | None,
        model_name: str,
        lang: str = "my",
        update_interval: float = 5.0,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.model_name = model_name
        self.lang = lang
        self.update_interval = update_interval
        self.current_step = 1
        self.start_time = time.monotonic()
        self._frame_idx = 0
        self._last_rendered_text = ""
        self._ticker_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def update_step(self, step: int) -> None:
        """Advance step and immediately render without waiting for ticker."""
        async with self._lock:
            self.current_step = step
            await self._render()

    async def start_ticker(self, step: int | None = None) -> None:
        """Start cancellable background ticker loop for live animated updates."""
        if step is not None:
            self.current_step = step
        if self._ticker_task and not self._ticker_task.done():
            return
        self._ticker_task = asyncio.create_task(self._ticker_loop())

    async def stop_ticker(self) -> None:
        """Safely cancel and await the background ticker task in finally blocks."""
        if self._ticker_task and not self._ticker_task.done():
            self._ticker_task.cancel()
            try:
                await self._ticker_task
            except asyncio.CancelledError:
                pass
        self._ticker_task = None

    async def _ticker_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.update_interval)
                async with self._lock:
                    self._frame_idx = (self._frame_idx + 1) % len(self.SPINNER_FRAMES)
                    await self._render()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("Ticker loop exception for %s:%s: %s", self.chat_id, self.message_id, e)

    async def _render(self) -> None:
        if not self.bot or not self.chat_id or not self.message_id:
            return
        elapsed = int(time.monotonic() - self.start_time)
        spinner = self.SPINNER_FRAMES[self._frame_idx]
        text = render_stepper_progress(
            step=self.current_step,
            model_name=self.model_name,
            elapsed_seconds=elapsed,
            spinner=spinner,
            lang=self.lang,
        )
        if text == self._last_rendered_text:
            return
        ok = await safe_edit_status(self.bot, self.chat_id, self.message_id, text)
        if ok:
            self._last_rendered_text = text
