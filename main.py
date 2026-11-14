"""Application entrypoint wiring configuration, SQLite persistence, RBAC, OIDC, queue, and Telegram bot."""

import asyncio
import contextlib
import logging
import shutil
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

from src.bot.api_server import build_bot_instance, check_local_bot_api_reachable
from src.bot.keyboards.radar import build_release_notification_keyboard
from src.bot.middlewares.rbac import RbacMiddleware
from src.bot.notifier import TelegramAdminLogNotifier
from src.bot.oidc_server import (
    OidcCallbackCoordinator,
    run_hourly_oidc_sync_loop,
)
from src.bot.routers import build_root_router
from src.bot.routers.radar import format_release_notification_card
from src.bot.tunnel import CloudflareQuickTunnel
from src.bot.webapp_server import BotWebServer
from src.config.settings import AppSettings, load_settings
from src.core.deezer import DeezerReleaseProvider
from src.core.extractor import SevenZipArchiveExtractor
from src.core.fingerprint import ChromaprintFingerprinter
from src.core.lyrics import LrclibLyricsClient
from src.core.metadata import MusicBrainzMetadataService
from src.core.models import (
    Permission,
    ProviderServerRecord,
    SongRequestRecord,
    UserAccount,
    UserRole,
)
from src.core.oidc import AuthentikOidcService
from src.core.organizer import LibraryOrganizer
from src.core.pipeline import IngestionPipeline
from src.core.provider import MultiProviderService
from src.core.queue import IngestionQueueWorker
from src.core.radar import ReleaseRadarService
from src.core.rbac import GovernanceService, has_permission
from src.core.search import AlbumSearchService
from src.core.stream_cache import EphemeralStreamCache
from src.db.database import SqliteDatabaseManager
from src.db.repository import SqliteCatalogRepository

logger = logging.getLogger(__name__)

BOT_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand(command="start", description="Open main menu, status & SSO pairing"),
    BotCommand(command="songs", description="Browse, search, download, edit & export songs"),
    BotCommand(command="search", description="Search songs & albums across library and Deezer"),
    BotCommand(command="radar", description="New releases radar & followed artists"),
    BotCommand(command="follow", description="Follow an artist for release alerts (/follow <artist>)"),
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
    # Suppress noisy per-query / HTTP frame debug logs from third-party libraries
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("hpack").setLevel(logging.WARNING)


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


async def run_periodic_release_radar_loop(
    radar_service: ReleaseRadarService,
    repository: SqliteCatalogRepository,
    bot: Bot | None = None,
    interval_seconds: int = 43200,
    stop_event: asyncio.Event | None = None,
    run_on_startup: bool = False,
    settings: AppSettings | None = None,
) -> None:
    """Periodically scan monitored artists and notify users of new releases."""
    effective_stop = stop_event or asyncio.Event()
    if run_on_startup and not effective_stop.is_set():
        try:
            logger.info("Starting initial Release Radar scan...")
            await radar_service.scan_monitored_artists()
        except Exception as exc:
            logger.warning("Initial Release Radar scan failed: %s", exc)

    while not effective_stop.is_set():
        try:
            await asyncio.wait_for(effective_stop.wait(), timeout=max(60, interval_seconds))
            break
        except TimeoutError:
            pass
        try:
            logger.info("Executing scheduled Release Radar artist scan...")
            discovered, notifications = await radar_service.scan_monitored_artists()
            if bot is not None and notifications:
                delivered_ids: list[int] = []
                for notif in notifications:
                    rel = await repository.get_discovered_release(notif.release_id)
                    if rel is not None:
                        try:
                            prefs = await repository.get_user_preferences(notif.telegram_id)
                            if not prefs.notifications_enabled:
                                continue
                            matching_tracks = await repository.find_library_tracks_for_release(
                                rel.artist_name, rel.title
                            )
                            lib_track = matching_tracks[0] if matching_tracks else None
                            web_url = settings.web_app_url if settings else None
                            text = format_release_notification_card(rel, lib_track)
                            keyboard = build_release_notification_keyboard(
                                release=rel,
                                lib_track=lib_track,
                                web_app_url=web_url,
                                platform_priority=prefs.platform_priority_list,
                            )
                            await bot.send_message(
                                chat_id=notif.telegram_id,
                                text=text,
                                reply_markup=keyboard,
                                parse_mode="HTML",
                            )
                            if notif.id > 0:
                                delivered_ids.append(notif.id)
                        except Exception as n_exc:
                            logger.warning(
                                "Failed to send release alert to user %d: %s", notif.telegram_id, n_exc
                            )
                if delivered_ids:
                    await repository.mark_release_notifications_delivered(delivered_ids)
        except Exception as exc:
            logger.warning("Scheduled Release Radar scan failed: %s", exc)


async def start_vite_dev_server_if_needed(settings: AppSettings) -> asyncio.subprocess.Process | None:
    """Auto-start Vite dev server in webapp/ if in dev_mode and not already running on port 5173."""
    if not (settings.web_app_enabled and settings.web_app_dev_mode):
        return None

    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", 5173), timeout=0.15)
        writer.close()
        await writer.wait_closed()
        logger.info("Vite dev server is already running on http://127.0.0.1:5173")
        return None
    except Exception:
        pass

    webapp_dir = Path("webapp")
    if not (webapp_dir / "package.json").is_file():
        return None

    npm_path = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm_path:
        logger.info("Node/npm not detected in PATH; serving pre-built webapp/dist static assets.")
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            npm_path,
            "run",
            "dev",
            cwd=str(webapp_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info("Auto-started Vite dev server (PID %d) at http://127.0.0.1:5173", proc.pid)
        return proc
    except Exception as exc:
        logger.warning("Could not auto-start Vite dev server: %s", exc)
        return None


async def run_app(settings: AppSettings | None = None) -> None:
    """Initialize SQLite WAL storage, assemble RBAC/OIDC/Queue services, and start Telegram polling."""
    resolved_settings = settings or load_settings()
    configure_logging(resolved_settings.log_level)

    if resolved_settings.telegram_use_local_api:
        reachable = await check_local_bot_api_reachable(resolved_settings.telegram_local_api_url)
        if not reachable:
            logger.error(
                "Local Telegram Bot API server at %s is unreachable. "
                "Start it via 'docker compose up -d telegram-bot-api' or set "
                "TELEGRAM_USE_LOCAL_API=false in .env to use standard Telegram Bot API.",
                resolved_settings.telegram_local_api_url,
            )
            raise SystemExit(1)

    vite_proc = await start_vite_dev_server_if_needed(resolved_settings)

    quick_tunnel: CloudflareQuickTunnel | None = None
    if resolved_settings.use_cloudflared_tunnel and not resolved_settings.cloudflared_tunnel_url.strip():
        local_target = f"http://127.0.0.1:{resolved_settings.web_listen_port}"
        quick_tunnel = CloudflareQuickTunnel(target_url=local_target)
        discovered_url = await quick_tunnel.start(timeout_seconds=20.0)
        if discovered_url:
            resolved_settings.update_cloudflared_tunnel_url(discovered_url)
            logger.info("Cloudflare Quick Tunnel active. Web App URL: %s", resolved_settings.web_app_url)
            logger.info("Cloudflare Quick Tunnel OIDC Redirect URI: %s", resolved_settings.oidc_redirect_uri)
        else:
            logger.warning("Cloudflare Quick Tunnel could not be established; continuing with fallback URLs.")

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
    deezer_provider = DeezerReleaseProvider()
    radar_service = ReleaseRadarService(provider=deezer_provider, repository=repository)
    album_search_service = AlbumSearchService(catalog_repository=repository, radar_provider=deezer_provider)
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
        role_mapping=resolved_settings.oidc_role_mapping,
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
    dispatcher["radar_service"] = radar_service
    dispatcher["album_search_service"] = album_search_service

    async def _on_provider_artifact_ready(
        artifact_path: Path,
        req: SongRequestRecord,
        server: ProviderServerRecord,
    ) -> str:
        requester = await repository.get_user(req.telegram_id)
        if requester is None:
            requester = await governance_service.ensure_user(
                req.telegram_id,
                display_name=f"User {req.telegram_id}",
            )
        enqueue_actor = (
            requester
            if has_permission(requester, Permission.SUBMIT_INGESTION)
            else UserAccount(
                telegram_id=requester.telegram_id,
                display_name=requester.display_name,
                username=requester.username,
                role=UserRole.UPLOADER,
            )
        )
        job, _pos = await queue_worker.enqueue_job(
            user=enqueue_actor,
            source_path=artifact_path,
            original_filename=f"{req.artist_name} - {req.track_title}{artifact_path.suffix}",
        )
        logger.info(
            "Enqueued provisioned track from %s for request #%d -> job %s",
            server.name,
            req.id,
            job.job_id,
        )
        return job.job_id

    async def _notify_provider_requester(telegram_id: int, message_text: str) -> None:
        with contextlib.suppress(Exception):
            await bot.send_message(chat_id=telegram_id, text=message_text, parse_mode="HTML")

    stream_cache = EphemeralStreamCache(
        cache_dir=resolved_settings.storage_stream_cache_dir,
        ttl_seconds=resolved_settings.stream_cache_ttl_seconds,
        max_bytes=resolved_settings.stream_cache_max_mb * 1024 * 1024,
    )
    await stream_cache.evict_expired()

    multi_provider_service = MultiProviderService(
        repository=repository,
        notifier=notifier,
        staging_dir=resolved_settings.storage_scratch_dir / "provider_staging",
        auto_provision_uploader_requests=resolved_settings.auto_provision_uploader_requests,
        download_timeout_seconds=resolved_settings.provision_download_timeout_seconds,
        min_archival_quality=resolved_settings.min_archival_quality,
        on_artifact_ready=_on_provider_artifact_ready,
        notify_requester=_notify_provider_requester,
    )
    for cfg_prov in resolved_settings.effective_music_providers:
        existing_srv = await repository.get_provider_server(cfg_prov.id)
        prov_record = ProviderServerRecord(
            id=cfg_prov.id,
            name=cfg_prov.name,
            base_url=cfg_prov.base_url,
            api_token_encrypted=cfg_prov.api_token or None,
            priority=cfg_prov.priority,
            is_enabled=cfg_prov.is_enabled,
            capabilities=existing_srv.capabilities if existing_srv else None,
            last_health_status=existing_srv.last_health_status if existing_srv else "unknown",
            last_health_checked_at=existing_srv.last_health_checked_at if existing_srv else None,
            allow_streaming=cfg_prov.allow_streaming,
            allow_archiving=cfg_prov.allow_archiving,
            min_archival_quality=cfg_prov.min_archival_quality,
        )
        if existing_srv is not None:
            prov_record.created_at = existing_srv.created_at
        try:
            updated_srv = await multi_provider_service.handshake_and_update(prov_record)
            logger.info(
                "Configured music provider: %s (%s) [stream=%s, archive=%s, min_archival=%s, status=%s]",
                updated_srv.name,
                updated_srv.base_url,
                updated_srv.allow_streaming,
                updated_srv.allow_archiving,
                updated_srv.min_archival_quality or resolved_settings.min_archival_quality,
                updated_srv.last_health_status,
            )
        except Exception as exc:
            logger.warning(
                "Music provider handshake failed for %s (%s): %s",
                cfg_prov.name,
                cfg_prov.base_url,
                exc,
            )

    dispatcher["multi_provider_service"] = multi_provider_service
    dispatcher["provider_service"] = multi_provider_service
    dispatcher.include_router(build_root_router())

    web_server: BotWebServer | None = None
    oidc_sync_task: asyncio.Task[None] | None = None
    radar_task: asyncio.Task[None] | None = None
    background_stop_event = asyncio.Event()
    rescan_task = asyncio.create_task(
        run_periodic_library_rescan_loop(
            organizer=organizer,
            repository=repository,
            interval_seconds=resolved_settings.oidc_sync_interval_seconds,
            stop_event=background_stop_event,
        )
    )
    queue_task = asyncio.create_task(queue_worker.run_worker_loop(stop_event=background_stop_event))
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

    if resolved_settings.radar_enabled:
        radar_task = asyncio.create_task(
            run_periodic_release_radar_loop(
                radar_service=radar_service,
                repository=repository,
                bot=bot,
                interval_seconds=resolved_settings.radar_sync_interval_seconds,
                stop_event=background_stop_event,
                settings=resolved_settings,
            )
        )

    if resolved_settings.web_app_enabled or resolved_settings.oidc_enabled:
        meta_service = getattr(pipeline, "_metadata", None)
        lyrics_client = getattr(pipeline, "_lyrics", None)
        bot_username: str | None = None
        with contextlib.suppress(Exception):
            bot_info = await bot.get_me()
            bot_username = bot_info.username

        web_server = BotWebServer(
            bot_token=resolved_settings.telegram_bot_token,
            governance_service=governance_service,
            coordinator=coordinator,
            static_dir=resolved_settings.web_app_static_dir,
            host=resolved_settings.web_listen_host,
            port=resolved_settings.web_listen_port,
            owner_telegram_ids=resolved_settings.owner_telegram_ids,
            dev_mode=resolved_settings.web_app_dev_mode,
            catalog_repository=repository,
            organizer=organizer,
            metadata_service=meta_service if isinstance(meta_service, MusicBrainzMetadataService) else None,
            lyrics_client=lyrics_client if isinstance(lyrics_client, LrclibLyricsClient) else None,
            queue_worker=queue_worker,
            pipeline=pipeline,
            scratch_dir=resolved_settings.storage_scratch_dir,
            radar_service=radar_service,
            album_search_service=album_search_service,
            bot=bot,
            bot_username=bot_username,
            provider_service=multi_provider_service,
            stream_cache=stream_cache,
        )
        await web_server.start()

    await register_bot_commands(bot, settings=resolved_settings)
    logger.info("Starting Music Ingestion Bot polling loop...")
    try:
        await dispatcher.start_polling(bot)
    finally:
        background_stop_event.set()
        queue_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await queue_task
        rescan_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await rescan_task
        if oidc_sync_task is not None:
            oidc_sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await oidc_sync_task
        if radar_task is not None:
            radar_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await radar_task
        if web_server is not None:
            await web_server.stop()
        if vite_proc is not None:
            try:
                vite_proc.terminate()
                await asyncio.wait_for(vite_proc.wait(), timeout=3.0)
            except Exception:
                with contextlib.suppress(Exception):
                    vite_proc.kill()
        if quick_tunnel is not None:
            await quick_tunnel.stop()
        await bot.session.close()


def main() -> None:
    """Synchronous CLI entrypoint."""
    asyncio.run(run_app())


if __name__ == "__main__":
    main()
