export interface TrackItem {
  id: string;
  title: string;
  artist: string;
  album: string;
  year: number | null;
  track_number: number | null;
  duration_seconds: number;
  has_synced_lyrics: boolean;
  format: string;
  audio_url: string;
  cover_url: string;
  file_size?: number;
  bitrate_kbps?: number;
}

export interface TracksResponse {
  items: TrackItem[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
}

export interface LyricsResponse {
  track_id: string;
  has_synced: boolean;
  synced_lyrics: string | null;
  plain_lyrics: string | null;
}

export interface ParsedLrcLine {
  time: number; // in seconds
  text: string;
}

export interface ReleaseTrackPreview {
  track_id: string;
  title: string;
  preview_url: string;
  duration_seconds: number;
  track_position: number;
  library_track_id?: string | null;
}

export interface RadarRelease {
  id: string;
  artist_name: string;
  title: string;
  release_type: string;
  release_date: string;
  cover_url: string | null;
  preview_url: string | null;
  external_url: string | null;
  provider: string;
  track_count: number;
  in_library: boolean;
  library_track_id?: string | null;
  is_followed: boolean;
  external_links?: Record<string, string>;
  primary_platform?: string;
  primary_url?: string;
}

export interface RadarFollowItem {
  id: number;
  artist_name: string;
  notify_telegram: boolean;
  created_at: string;
}

export interface RadarScanProgress {
  is_scanning: boolean;
  current_artist: string;
  processed_artists: number;
  total_artists: number;
  discovered_count: number;
  started_at: string | null;
  percent: number;
}

export interface ArtistDetailsResponse {
  artist_name: string;
  is_followed: boolean;
  library_tracks: TrackItem[];
  discovered_releases: RadarRelease[];
  stats: {
    tracks_in_library: number;
    releases_discovered: number;
  };
}


export interface AlbumTrackGap {
  position: number;
  title: string;
  duration_seconds: number;
  is_owned: boolean;
  library_track_id: string | null;
  preview_url: string | null;
  disk_number?: number;
}

export interface AlbumGapAnalysis {
  album_id: string;
  title: string;
  artist: string;
  cover_url: string | null;
  release_date: string | null;
  total_tracks: number;
  owned_tracks_count: number;
  missing_tracks_count: number;
  coverage_percent: number;
  is_complete: boolean;
  provider: string;
  external_url: string | null;
  tracks: AlbumTrackGap[];
}

export interface SearchResultAlbum {
  id: string;
  title: string;
  artist: string;
  cover_url: string | null;
  in_library: boolean;
  total_tracks: number;
  owned_tracks_count: number;
  missing_tracks_count: number;
  external_url: string | null;
}

export interface SearchResultTrack {
  id: string;
  title: string;
  artist: string;
  album: string;
  in_library: boolean;
  library_track_id: string | null;
  preview_url: string | null;
  duration_seconds: number;
}

export interface AppSearchResponse {
  query: string;
  scope: 'global' | 'library';
  albums: SearchResultAlbum[];
  tracks: SearchResultTrack[];
}

export interface UserSettings {
  telegram_id: number;
  stream_format: string;
  stream_bitrate: number;
  auto_lyrics_search: boolean;
  theme: string;
  streaming_platform_order: string;
  search_scope: 'global' | 'library';
  notifications_enabled: boolean;
}

