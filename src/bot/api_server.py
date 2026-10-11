"""Local Telegram Bot API session factory and container-to-host file path resolver."""

import logging
from pathlib import Path, PurePosixPath

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import PRODUCTION, TelegramAPIServer
from aiogram.enums import ParseMode

from src.config.settings import AppSettings

logger = logging.getLogger(__name__)


def build_telegram_api_server(settings: AppSettings) -> TelegramAPIServer:
    """Return a custom `--local` `TelegramAPIServer` or standard Telegram production server."""
    if settings.telegram_use_local_api:
        logger.info(
            "Configuring aiogram for local Telegram Bot API server at %s (is_local=True)",
            settings.telegram_local_api_url,
        )
        return TelegramAPIServer.from_base(
            settings.telegram_local_api_url,
            is_local=True,
        )
    logger.info("Configuring aiogram for standard production Telegram Bot API server")
    return PRODUCTION


def build_bot_instance(settings: AppSettings) -> Bot:
    """Instantiate an `aiogram.Bot` wired to the configured Telegram Bot API endpoint."""
    api_server = build_telegram_api_server(settings)
    session = AiohttpSession(api=api_server)
    return Bot(
        token=settings.telegram_bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def resolve_local_api_file_path(
    reported_file_path: str | Path,
    settings: AppSettings,
) -> Path:
    """Translate a file path returned by `telegram-bot-api --local` into an accessible local path.

    When `telegram-bot-api` runs inside WSL Docker with volume mount
    `./storage/telegram-bot-api:/var/lib/telegram-bot-api`, `getFile` returns POSIX paths
    starting with `/var/lib/telegram-bot-api/<token>/...`.
    - Inside the bot Docker container, both paths are `/var/lib/telegram-bot-api`, so it resolves
      directly.
    - In hybrid mode (bot running on host/WSL via `uv`), this function maps the container prefix
      `/var/lib/telegram-bot-api` onto `settings.telegram_local_api_host_dir`.
    """
    raw_posix = PurePosixPath(str(reported_file_path).replace("\\", "/"))
    work_dir_posix = PurePosixPath(str(settings.telegram_local_api_work_dir).replace("\\", "/"))

    try:
        relative_part = raw_posix.relative_to(work_dir_posix)
        resolved = settings.telegram_local_api_host_dir / Path(*relative_part.parts)
        logger.debug(
            "Translated local Bot API path %s -> %s",
            reported_file_path,
            resolved,
        )
        return resolved
    except ValueError:
        return Path(str(reported_file_path))
