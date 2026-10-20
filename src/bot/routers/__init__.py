"""Router aggregation for the Telegram bot interface layer."""

from aiogram import Router

from src.bot.routers.admin import admin_router
from src.bot.routers.commands import commands_router
from src.bot.routers.files import files_router
from src.bot.routers.radar import radar_router
from src.bot.routers.songs import songs_router
from src.bot.routers.verification import verification_router


def build_root_router() -> Router:
    """Assemble and return the top-level aiogram Router with all sub-routers attached."""
    root = Router(name="root")
    for sub_router in (
        commands_router,
        admin_router,
        songs_router,
        radar_router,
        files_router,
        verification_router,
    ):
        if sub_router.parent_router is not None:
            sub_router._parent_router = None
        root.include_router(sub_router)
    return root


__all__ = [
    "admin_router",
    "build_root_router",
    "commands_router",
    "files_router",
    "radar_router",
    "songs_router",
    "verification_router",
]
