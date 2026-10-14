from __future__ import annotations

from typing import Any

from aiogram import Dispatcher
from app.middlewares.access_control import AccessControlMiddleware


def setup_middlewares(dp: Dispatcher, session_factory: Any = None) -> None:
    """Register all outer/inner middlewares on the dispatcher."""
    for m in dp.message.outer_middleware:
        if isinstance(m, AccessControlMiddleware):
            return
    access_middleware = AccessControlMiddleware(session_factory=session_factory)
    dp.message.outer_middleware(access_middleware)
    dp.callback_query.outer_middleware(access_middleware)


__all__ = ["AccessControlMiddleware", "setup_middlewares"]
