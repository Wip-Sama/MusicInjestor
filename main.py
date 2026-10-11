"""Application entrypoint wiring configuration, SQLite persistence, RBAC, OIDC, queue, and Telegram bot."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from src.bot.api_server import build_bot_instance
from src.bot.middlewares.rbac import RbacMiddleware
from src.bot.notifier import TelegramAdminLogNotifier
from src.bot.routers import build_root_router
from src.config.settings import AppSettings, load_settings
from src.core.extractor import SevenZipArchiveExtractor
from src.core.fingerprint import ChromaprintFingerprinter
from src.core.lyrics import LrclibLyricsClient
from src.core.metadata import MusicBrainzMetadataService
from src.core.oidc import AuthentikOidcService
from src.core.organizer import LibraryOrganizer
from src.core.pipeline import IngestionPipeline
from src.core.queue import IngestionQueueWorker
from src.core.rbac import GovernanceService
from src.db.database import SqliteDatabaseManager
from src.db.repository import SqliteCatalogRepository

logger = logging.getLogger(__name__)

BOT_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand(command="start", description="Open main menu, status & SSO pairing"),
    BotCommand(command="admin", description="Open Administrator Control Panel (ADMIN+)"),
    BotCommand(command="adduser", description="Authorize user by Telegram ID (ADMIN+)"),
    BotCommand(command="queue", description="View active & queued ingestion jobs"),
    BotCommand(command="status", description="Check system, RBAC & queue status"),
    BotCommand(command="help", description="Show usage instructions & commands"),
)


def configure_logging(level_name: str) -> None:
    """Configure root logging with English timestamps and module names."""
    numeric_level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


def create_ingestion_pipeline(
    settings: AppSettings,
    db_manager: SqliteDatabaseManager,
) -> IngestionPipeline:
    """Instantiate the pure domain `IngestionPipeline` wired with concrete infrastructure adapters."""
    settings.ensure_directories()
    extractor = SevenZipArchiveExtractor(
        scratch_root=settings.storage_scratch_dir,
        sevenzip_binary=settings.sevenzip_binary,
    )
    fingerprinter = ChromaprintFingerprinter(
        fpcalc_binary=settings.fpcalc_binary,
        acoustid_api_key=settings.acoustid_api_key,
    )
    metadata_provider = MusicBrainzMetadataService(
        app_name=settings.musicbrainz_app_name,
        app_version=settings.musicbrainz_app_version,
        contact=settings.musicbrainz_contact,
    )
    lyrics_provider = LrclibLyricsClient(base_url=settings.lrclib_base_url)
    organizer = LibraryOrganizer(library_root=settings.storage_library_dir)
    repository = SqliteCatalogRepository(db_manager=db_manager)

    return IngestionPipeline(
        extractor=extractor,
        fingerprinter=fingerprinter,
        metadata_provider=metadata_provider,
        lyrics_provider=lyrics_provider,
        organizer=organizer,
        catalog_repository=repository,
        auto_approve_confidence=settings.auto_approve_confidence,
    )


async def register_bot_commands(bot: Bot) -> None:
    """Register the bot's slash command menu in Telegram."""
    try:
        await bot.set_my_commands(list(BOT_COMMANDS))
        logger.info("Registered %d Telegram bot commands.", len(BOT_COMMANDS))
    except Exception as exc:
        logger.warning("Could not register bot command menu: %s", exc)


async def run_app(settings: AppSettings | None = None) -> None:
    """Initialize SQLite WAL storage, assemble RBAC/OIDC/Queue services, and start Telegram polling."""
    resolved_settings = settings or load_settings()
    configure_logging(resolved_settings.log_level)

    db_manager = SqliteDatabaseManager(resolved_settings.database_path)
    await db_manager.initialize()
    repository = SqliteCatalogRepository(db_manager=db_manager)
    pipeline = create_ingestion_pipeline(resolved_settings, db_manager)

    bot = build_bot_instance(resolved_settings)
    notifier = TelegramAdminLogNotifier(
        bot=bot,
        admin_log_channel_id=resolved_settings.admin_log_channel_id,
    )
    governance_service = GovernanceService(
        repository=repository,
        notifier=notifier,
        owner_telegram_ids=resolved_settings.owner_telegram_ids,
    )
    oidc_service = AuthentikOidcService(
        repository=repository,
        secret_key=resolved_settings.secret_key,
        authorize_url=resolved_settings.oidc_authorize_url,
        token_url=resolved_settings.oidc_token_url,
        userinfo_url=resolved_settings.oidc_userinfo_url,
        client_id=resolved_settings.oidc_client_id,
        client_secret=resolved_settings.oidc_client_secret,
        redirect_uri=resolved_settings.oidc_redirect_uri,
        notifier=notifier,
    )
    queue_worker = IngestionQueueWorker(
        pipeline=pipeline,
        catalog_repository=repository,
        governance_repository=repository,
        notifier=notifier,
        concurrency_limit=resolved_settings.queue_concurrency_limit,
    )
    await queue_worker.recover_pending_jobs()

    rbac_middleware = RbacMiddleware(
        governance_service=governance_service,
        oidc_service=oidc_service,
        oidc_enabled=resolved_settings.oidc_enabled,
    )

    dispatcher = Dispatcher()
    dispatcher.message.outer_middleware(rbac_middleware)
    dispatcher.callback_query.outer_middleware(rbac_middleware)
    dispatcher["pipeline"] = pipeline
    dispatcher["settings"] = resolved_settings
    dispatcher["governance_repository"] = repository
    dispatcher["governance_service"] = governance_service
    dispatcher["oidc_service"] = oidc_service
    dispatcher["queue_worker"] = queue_worker
    dispatcher["notifier"] = notifier
    dispatcher.include_router(build_root_router())

    await register_bot_commands(bot)
    logger.info("Starting Music Ingestion Bot polling loop...")
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


def main() -> None:
    """Synchronous CLI entrypoint."""
    asyncio.run(run_app())


if __name__ == "__main__":
    main()
