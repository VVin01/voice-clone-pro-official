from aiogram import Dispatcher
from app.handlers.admin import admin_router
from app.handlers.clone import clone_router
from app.handlers.menu import menu_router
from app.handlers.payment import payment_router
from app.handlers.start import start_router


def setup_handlers(dp: Dispatcher) -> None:
    dp.include_router(start_router)
    dp.include_router(menu_router)
    dp.include_router(clone_router)
    dp.include_router(payment_router)
    dp.include_router(admin_router)
