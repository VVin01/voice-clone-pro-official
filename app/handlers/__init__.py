from __future__ import annotations
from typing import Any
from aiogram import Dispatcher
from app.handlers.admin import admin_router
from app.handlers.clone import clone_router
from app.handlers.menu import menu_router
from app.handlers.payment import payment_router
from app.handlers.start import start_router
from app.middlewares import setup_middlewares


def setup_handlers(dp: Dispatcher, session_factory: Any = None) -> None:
    setup_middlewares(dp, session_factory=session_factory)
    for router in (start_router, menu_router, clone_router, payment_router, admin_router):
        if router.parent_router is not dp:
            router._parent_router = None
            dp.include_router(router)
