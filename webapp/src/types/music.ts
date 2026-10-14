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

