"""Router aggregation for the Telegram bot interface layer."""

from aiogram import Router

from src.bot.routers.admin import admin_router
from src.bot.routers.commands import commands_router
from src.bot.routers.files import files_router
from src.bot.routers.verification import verification_router


def build_root_router() -> Router:
    """Assemble and return the top-level aiogram Router with all sub-routers attached."""
    root = Router(name="root")
    root.include_router(commands_router)
    root.include_router(admin_router)
    root.include_router(files_router)
    root.include_router(verification_router)
    return root


__all__ = [
    "admin_router",
    "build_root_router",
    "commands_router",
    "files_router",
    "verification_router",
]
