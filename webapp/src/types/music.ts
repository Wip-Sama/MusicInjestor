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
