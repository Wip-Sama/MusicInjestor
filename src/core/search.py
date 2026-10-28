"""Album gap analysis and unified catalog/global search domain service."""

import asyncio
import logging
import re

from src.core.models import (
    AlbumGapAnalysis,
    AlbumTrackGap,
    DiscoveredRelease,
    LibraryTrackRecord,
    ReleaseTrackPreview,
    SearchResultItem,
    UnifiedSearchResult,
)
from src.core.ports import GovernanceRepositoryPort, ReleaseRadarProviderPort

logger = logging.getLogger(__name__)


def _normalize_title(val: str) -> str:
    """Normalize a title or artist name for fuzzy comparison."""
    return re.sub(r"[^\w\s]", "", val.lower()).strip()


class AlbumSearchService:
    """Provides album gap analysis, library search, and global catalog search with preview integration."""

    def __init__(
        self,
        catalog_repository: GovernanceRepositoryPort,
        radar_provider: ReleaseRadarProviderPort | None = None,
    ) -> None:
        self._repository = catalog_repository
        self._provider = radar_provider

    async def analyze_album_gap(
        self,
        artist: str,
        album: str,
        provider_release_id: str | None = None,
    ) -> AlbumGapAnalysis:
        """Calculate owned vs missing tracks for an album and attach preview snippets for missing tracks."""
        clean_artist = artist.strip()
        clean_album = album.strip()
        album_id = (
            f"deezer:{provider_release_id}"
            if provider_release_id
            else f"local:{clean_artist.lower()}:{clean_album.lower()}"
        )

        # 1. Fetch library tracks matching this artist and album
        lib_tracks = await self._repository.list_album_tracks(clean_artist, clean_album)
        lib_by_pos: dict[int, LibraryTrackRecord] = {
            t.track_number: t for t in lib_tracks if t.track_number is not None and t.track_number > 0
        }
        lib_by_title: dict[str, LibraryTrackRecord] = {
            _normalize_title(t.title): t for t in lib_tracks if t.title
        }

        # 2. Fetch canonical tracklist from provider
        canonical_tracks: list[ReleaseTrackPreview] = []
        cover_url: str | None = None
        release_date: str | None = None
        external_url: str | None = None

        target_provider_id = provider_release_id
        if not target_provider_id and self._provider is not None:
            # Query provider for matching album
            try:
                candidate_albums = await self._provider.search_albums(
                    f"{clean_artist} {clean_album}", limit=5
                )
                norm_art = _normalize_title(clean_artist)
                norm_alb = _normalize_title(clean_album)
                for cand in candidate_albums:
                    if (
                        _normalize_title(cand.artist_name) == norm_art
                        and _normalize_title(cand.title) == norm_alb
                    ):
                        target_provider_id = cand.provider_release_id
                        cover_url = cand.cover_url
                        external_url = cand.external_url
                        album_id = f"deezer:{target_provider_id}"
                        break
                if not target_provider_id and candidate_albums:
                    target_provider_id = candidate_albums[0].provider_release_id
                    cover_url = candidate_albums[0].cover_url
                    external_url = candidate_albums[0].external_url
                    album_id = f"deezer:{target_provider_id}"
            except Exception as exc:
                logger.warning(
                    "Failed to look up provider album for '%s - %s': %s", clean_artist, clean_album, exc
                )

        if target_provider_id and self._provider is not None:
            try:
                canonical_tracks = await self._provider.get_release_track_previews(target_provider_id)
            except Exception as exc:
                logger.warning(
                    "Failed to fetch canonical previews for release %s: %s", target_provider_id, exc
                )

        # 3. Reconcile owned vs missing
        gap_tracks: list[AlbumTrackGap] = []
        if canonical_tracks:
            for c_trk in canonical_tracks:
                pos = c_trk.track_position
                norm_t = _normalize_title(c_trk.title)
                matched_lib: LibraryTrackRecord | None = lib_by_pos.get(pos) or lib_by_title.get(norm_t)

                if matched_lib is not None:
                    gap_tracks.append(
                        AlbumTrackGap(
                            position=pos,
                            title=c_trk.title,
                            duration_seconds=c_trk.duration_seconds,
                            is_owned=True,
                            library_track_id=matched_lib.id,
                            preview_url=c_trk.preview_url,
                        )
                    )
                else:
                    gap_tracks.append(
                        AlbumTrackGap(
                            position=pos,
                            title=c_trk.title,
                            duration_seconds=c_trk.duration_seconds,
                            is_owned=False,
                            library_track_id=None,
                            preview_url=c_trk.preview_url,
                        )
                    )
        else:
            # Fallback when provider has no tracks: build purely from library
            for idx, lib_trk in enumerate(lib_tracks):
                pos = lib_trk.track_number or idx + 1
                gap_tracks.append(
                    AlbumTrackGap(
                        position=pos,
                        title=lib_trk.title,
                        duration_seconds=0,
                        is_owned=True,
                        library_track_id=lib_trk.id,
                        preview_url=None,
                    )
                )

        total_tracks = len(gap_tracks)
        owned_count = sum(1 for t in gap_tracks if t.is_owned)
        missing_count = total_tracks - owned_count

        return AlbumGapAnalysis(
            album_id=album_id,
            title=clean_album,
            artist=clean_artist,
            cover_url=cover_url,
            release_date=release_date,
            total_tracks=total_tracks,
            owned_tracks_count=owned_count,
            missing_tracks_count=missing_count,
            tracks=gap_tracks,
            provider="deezer" if target_provider_id else "local",
            external_url=external_url,
        )

    async def search(
        self,
        query: str,
        scope: str = "global",
        limit: int = 20,
    ) -> UnifiedSearchResult:
        """Perform unified search across albums and tracks according to active scope."""
        clean_q = query.strip()
        normalized_scope = "library" if scope.lower() in ("library", "library_only", "local") else "global"

        if not clean_q:
            return UnifiedSearchResult(query=clean_q, scope=normalized_scope)

        if normalized_scope == "library":
            return await self._search_library_only(clean_q, limit=limit)
        return await self._search_global(clean_q, limit=limit)

    async def _search_library_only(self, query: str, limit: int = 20) -> UnifiedSearchResult:
        """Search local library tracks and distinct albums, enriched with gap analysis previews."""
        matched_tracks = await self._repository.search_library_tracks(query, limit=limit)
        track_items = [
            SearchResultItem(
                item_type="track",
                id=t.id,
                title=t.title,
                artist=t.artist,
                album=t.album,
                in_library=True,
                library_track_id=t.id,
                preview_url=None,
                duration_seconds=0,
            )
            for t in matched_tracks
        ]

        # Distinct albums from library matching query
        matched_albums = await self._repository.search_library_albums(query, limit=10)
        album_items: list[SearchResultItem] = []
        for art, alb, _cnt, _yr in matched_albums:
            gap = await self.analyze_album_gap(art, alb)
            album_items.append(
                SearchResultItem(
                    item_type="album",
                    id=gap.album_id,
                    title=gap.title,
                    artist=gap.artist,
                    album=gap.title,
                    cover_url=gap.cover_url,
                    in_library=gap.is_complete,
                    total_tracks=gap.total_tracks,
                    owned_tracks_count=gap.owned_tracks_count,
                    missing_tracks_count=gap.missing_tracks_count,
                    external_url=gap.external_url,
                )
            )

        return UnifiedSearchResult(
            query=query,
            scope="library",
            albums=album_items,
            tracks=track_items,
        )

    async def _search_global(self, query: str, limit: int = 20) -> UnifiedSearchResult:
        """Search both local library and external provider (Deezer) with track reconciliation and previews."""
        if self._provider is None:
            return await self._search_library_only(query, limit=limit)

        # 1. Parallel fetch from local library and external provider
        lib_tracks_task = self._repository.search_library_tracks(query, limit=limit)
        lib_albums_task = self._repository.search_library_albums(query, limit=10)
        ext_albums_task = self._provider.search_albums(query, limit=10)
        ext_tracks_task = self._provider.search_tracks(query, limit=15)

        results = await asyncio.gather(
            lib_tracks_task,
            lib_albums_task,
            ext_albums_task,
            ext_tracks_task,
            return_exceptions=True,
        )

        lib_tracks: list[LibraryTrackRecord] = results[0] if isinstance(results[0], list) else []
        lib_albums: list[tuple[str, str, int, int | None]] = (
            results[1] if isinstance(results[1], list) else []
        )
        ext_albums: list[DiscoveredRelease] = results[2] if isinstance(results[2], list) else []
        ext_tracks: list[ReleaseTrackPreview] = results[3] if isinstance(results[3], list) else []

        # 2. Reconcile tracks
        lib_titles = {_normalize_title(t.title): t for t in lib_tracks}
        final_tracks: list[SearchResultItem] = [
            SearchResultItem(
                item_type="track",
                id=t.id,
                title=t.title,
                artist=t.artist,
                album=t.album,
                in_library=True,
                library_track_id=t.id,
                preview_url=None,
                duration_seconds=0,
            )
            for t in lib_tracks
        ]

        for ext_tr in ext_tracks:
            norm_t = _normalize_title(ext_tr.title)
            # If not already owned in library, add as a previewable missing track
            if norm_t not in lib_titles:
                final_tracks.append(
                    SearchResultItem(
                        item_type="track",
                        id=f"preview:{ext_tr.track_id}",
                        title=ext_tr.title,
                        artist="",
                        in_library=False,
                        library_track_id=None,
                        preview_url=ext_tr.preview_url,
                        duration_seconds=ext_tr.duration_seconds,
                    )
                )

        # 3. Reconcile albums with gap analysis
        final_albums: list[SearchResultItem] = []
        seen_albums: set[str] = set()

        for ext_alb in ext_albums:
            alb_key = f"{_normalize_title(ext_alb.artist_name)}:{_normalize_title(ext_alb.title)}"
            seen_albums.add(alb_key)
            gap = await self.analyze_album_gap(
                ext_alb.artist_name,
                ext_alb.title,
                provider_release_id=ext_alb.provider_release_id,
            )
            final_albums.append(
                SearchResultItem(
                    item_type="album",
                    id=gap.album_id,
                    title=gap.title,
                    artist=gap.artist,
                    album=gap.title,
                    cover_url=gap.cover_url or ext_alb.cover_url,
                    in_library=gap.is_complete,
                    total_tracks=gap.total_tracks,
                    owned_tracks_count=gap.owned_tracks_count,
                    missing_tracks_count=gap.missing_tracks_count,
                    external_url=gap.external_url or ext_alb.external_url,
                )
            )

        # Include local library albums not caught in external results
        for art, alb, _cnt, _yr in lib_albums:
            alb_key = f"{_normalize_title(art)}:{_normalize_title(alb)}"
            if alb_key not in seen_albums:
                seen_albums.add(alb_key)
                gap = await self.analyze_album_gap(art, alb)
                final_albums.append(
                    SearchResultItem(
                        item_type="album",
                        id=gap.album_id,
                        title=gap.title,
                        artist=gap.artist,
                        album=gap.title,
                        cover_url=gap.cover_url,
                        in_library=gap.is_complete,
                        total_tracks=gap.total_tracks,
                        owned_tracks_count=gap.owned_tracks_count,
                        missing_tracks_count=gap.missing_tracks_count,
                        external_url=gap.external_url,
                    )
                )

        return UnifiedSearchResult(
            query=query,
            scope="global",
            albums=final_albums[:limit],
            tracks=final_tracks[:limit],
        )
