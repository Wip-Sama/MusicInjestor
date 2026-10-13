"""Application entrypoint wiring configuration, SQLite persistence, RBAC, OIDC, queue, and Telegram bot."""

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

from src.bot.api_server import build_bot_instance
from src.bot.middlewares.rbac import RbacMiddleware
from src.bot.notifier import TelegramAdminLogNotifier
from src.bot.oidc_server import (
    OidcCallbackCoordinator,
    run_hourly_oidc_sync_loop,
)
from src.bot.routers import build_root_router
from src.bot.webapp_server import BotWebServer
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
    BotCommand(command="songs", description="Browse, search, download, edit & export songs"),
    BotCommand(command="rescan", description="Scan library folder & sync database"),
    BotCommand(command="job", description="Inspect, pause, unlock or review a job by ID"),
    BotCommand(command="cancel", description="Exit any active input or confirmation prompt"),
    BotCommand(command="admin", description="Open Administrator Control Panel (ADMIN+)"),
    BotCommand(command="adduser", description="Authorize user by Telegram ID (ADMIN+)"),
    BotCommand(command="fixtags", description="Request post-ingestion tag edits"),
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
        genius_access_token=settings.genius_access_token,
        lastfm_api_key=settings.lastfm_api_key,
        enable_network=True,
    )
    lyrics_provider = LrclibLyricsClient(base_url=settings.lrclib_base_url)
    organizer = LibraryOrganizer(
        library_root=settings.storage_library_dir,
        umask=settings.umask,
        enable_network=True,
    )
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


async def register_bot_commands(bot: Bot, settings: AppSettings | None = None) -> None:
    """Register the bot's slash command menu and persistent Web App Menu Button in Telegram."""
    try:
        await bot.set_my_commands(list(BOT_COMMANDS))
        logger.info("Registered %d Telegram bot commands.", len(BOT_COMMANDS))
    except Exception as exc:
        logger.warning("Could not register bot command menu: %s", exc)

    if settings is not None and settings.web_app_url.strip():
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="🎵 Web App",
                    web_app=WebAppInfo(url=settings.web_app_url.strip()),
                )
            )
            logger.info("Configured persistent Telegram Menu Button for Mini App: %s", settings.web_app_url)
        except Exception as exc:
            logger.warning("Could not set Web App menu button: %s", exc)


async def run_periodic_library_rescan_loop(
    organizer: LibraryOrganizer,
    repository: SqliteCatalogRepository,
    interval_seconds: int = 3600,
    stop_event: asyncio.Event | None = None,
    run_on_startup: bool = True,
) -> None:
    """Periodically scan `storage/library` and reconcile filesystem changes with SQLite."""
    effective_stop = stop_event or asyncio.Event()
    if run_on_startup and not effective_stop.is_set():
        try:
            logger.info("Starting background library reconciliation for %s...", organizer.library_root)
            report = await organizer.reconcile_library_with_repository(repository)
            logger.info(
                "Library reconciliation complete: %d unchanged, +%d added, ~%d updated, -%d removed.",
                report.unchanged_count,
                len(report.added_tracks),
                len(report.updated_tracks),
                len(report.removed_tracks),
            )
        except Exception as exc:
            logger.warning("Initial library reconciliation failed: %s", exc)

    while not effective_stop.is_set():
        try:
            await asyncio.wait_for(effective_stop.wait(), timeout=max(1, interval_seconds))
            break
        except TimeoutError:
            pass
        try:
            report = await organizer.reconcile_library_with_repository(repository)
            if report.has_changes:
                logger.info(
                    "Periodic library rescan synchronized changes: +%d added, ~%d updated, -%d removed.",
                    len(report.added_tracks),
                    len(report.updated_tracks),
                    len(report.removed_tracks),
                )
        except Exception as exc:
            logger.warning("Periodic library rescan failed: %s", exc)


async def run_app(settings: AppSettings | None = None) -> None:
    """Initialize SQLite WAL storage, assemble RBAC/OIDC/Queue services, and start Telegram polling."""
    resolved_settings = settings or load_settings()
    configure_logging(resolved_settings.log_level)

    db_manager = SqliteDatabaseManager(resolved_settings.database_path)
    await db_manager.initialize()
    repository = SqliteCatalogRepository(db_manager=db_manager)
    pipeline = create_ingestion_pipeline(resolved_settings, db_manager)

    organizer = getattr(pipeline, "_organizer", None)
    if not isinstance(organizer, LibraryOrganizer):
        organizer = LibraryOrganizer(
            library_root=resolved_settings.storage_library_dir,
            umask=resolved_settings.umask,
            enable_network=True,
        )

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
    dispatcher["catalog_repository"] = repository
    dispatcher["governance_repository"] = repository
    dispatcher["governance_service"] = governance_service
    dispatcher["oidc_service"] = oidc_service
    dispatcher["queue_worker"] = queue_worker
    dispatcher["notifier"] = notifier
    dispatcher["organizer"] = organizer
    dispatcher.include_router(build_root_router())

    web_server: BotWebServer | None = None
    oidc_sync_task: asyncio.Task[None] | None = None
    background_stop_event = asyncio.Event()
    rescan_task = asyncio.create_task(
        run_periodic_library_rescan_loop(
            organizer=organizer,
            repository=repository,
            interval_seconds=resolved_settings.oidc_sync_interval_seconds,
            stop_event=background_stop_event,
        )
    )
    coordinator: OidcCallbackCoordinator | None = None
    if resolved_settings.oidc_enabled:
        coordinator = OidcCallbackCoordinator(
            oidc_service=oidc_service,
            bot=bot,
            webhook_secret=resolved_settings.oidc_webhook_secret,
        )
        oidc_sync_task = asyncio.create_task(
            run_hourly_oidc_sync_loop(
                oidc_service=oidc_service,
                interval_seconds=resolved_settings.oidc_sync_interval_seconds,
                stop_event=background_stop_event,
            )
        )

    if resolved_settings.web_app_enabled or resolved_settings.oidc_enabled:
        web_server = BotWebServer(
            bot_token=resolved_settings.telegram_bot_token,
            governance_service=governance_service,
            coordinator=coordinator,
            static_dir=resolved_settings.web_app_static_dir,
            host=resolved_settings.web_listen_host,
            port=resolved_settings.web_listen_port,
            owner_telegram_ids=resolved_settings.owner_telegram_ids,
            dev_mode=resolved_settings.web_app_dev_mode,
        )
        await web_server.start()

    await register_bot_commands(bot, settings=resolved_settings)
    logger.info("Starting Music Ingestion Bot polling loop...")
    try:
        await dispatcher.start_polling(bot)
    finally:
        background_stop_event.set()
        rescan_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await rescan_task
        if oidc_sync_task is not None:
            oidc_sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await oidc_sync_task
        if web_server is not None:
            await web_server.stop()
        await bot.session.close()


def main() -> None:
    """Synchronous CLI entrypoint."""
    asyncio.run(run_app())


if __name__ == "__main__":
    main()
