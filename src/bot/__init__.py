"""Telegram interface package (aiogram 3.x) acting as a driving adapter for `src.core`."""

from src.bot.api_server import build_bot_instance, resolve_local_api_file_path
from src.bot.states import IngestionStates

__all__ = ["IngestionStates", "build_bot_instance", "resolve_local_api_file_path"]
