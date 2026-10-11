"""Application entrypoint wiring configuration, SQLite persistence, core pipeline, and Telegram bot."""

import asyncio
import logging

from aiogram import Dispatcher

from src.bot.api_server import build_bot_instance
from src.bot.routers import build_root_router
from src.config.settings import AppSettings, load_settings
from src.core.extractor import SevenZipArchiveExtractor
from src.core.fingerprint import ChromaprintFingerprinter
from src.core.lyrics import LrclibLyricsClient
from src.core.metadata import MusicBrainzMetadataService
from src.core.organizer import LibraryOrganizer
from src.core.pipeline import IngestionPipeline
from src.db.database import SqliteDatabaseManager
from src.db.repository import SqliteCatalogRepository

logger = logging.getLogger(__name__)


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


async def run_app(settings: AppSettings | None = None) -> None:
    """Initialize SQLite WAL storage, assemble the core pipeline, and start Telegram polling."""
    resolved_settings = settings or load_settings()
    configure_logging(resolved_settings.log_level)

    db_manager = SqliteDatabaseManager(resolved_settings.database_path)
    await db_manager.initialize()
    pipeline = create_ingestion_pipeline(resolved_settings, db_manager)

    bot = build_bot_instance(resolved_settings)
    dispatcher = Dispatcher()
    dispatcher["pipeline"] = pipeline
    dispatcher["settings"] = resolved_settings
    dispatcher.include_router(build_root_router())

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
