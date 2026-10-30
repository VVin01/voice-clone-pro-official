from __future__ import annotations

from typing import Any

from aiogram import Dispatcher
from app.middlewares.access_control import AccessControlMiddleware
from app.middlewares.membership_recheck import MembershipRecheckMiddleware


def setup_middlewares(dp: Dispatcher, session_factory: Any = None) -> None:
    """Register all outer/inner middlewares on the dispatcher.

    Order matters: AccessControl (BLOCKED accounts) runs first, then the
    TTL-cached membership re-check for already-verified users.
    """
    for m in dp.message.outer_middleware:
        if isinstance(m, AccessControlMiddleware):
            return
    access_middleware = AccessControlMiddleware(session_factory=session_factory)
    dp.message.outer_middleware(access_middleware)
    dp.callback_query.outer_middleware(access_middleware)

    recheck_middleware = MembershipRecheckMiddleware(session_factory=session_factory)
    dp.message.outer_middleware(recheck_middleware)
    dp.callback_query.outer_middleware(recheck_middleware)


__all__ = ["AccessControlMiddleware", "MembershipRecheckMiddleware", "setup_middlewares"]
