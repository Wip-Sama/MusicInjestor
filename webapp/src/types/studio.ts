export interface SongEditProposalPayload {
  track_id: string;
  action_type: string;
  new_title?: string;
  new_artist?: string;
  new_album?: string;
  new_year?: number | null;
  new_track_number?: number | null;
  new_lyrics?: string | null;
  new_cover_hex?: string | null;
  new_cover_mime?: string;
  new_recording_mbid?: string | null;
  new_release_mbid?: string | null;
  accepted_fields?: string[];
}

export interface AutoLyricsResponse {
  found: boolean;
  lyrics: string | null;
  synced: boolean;
  line_count: number;
  artist?: string;
  title?: string;
  message?: string;
}

export interface DiffItem {
  field: string;
  label: string;
  old: string | number | null;
  new: string | number | null;
  changed: boolean;
}

export interface ProposalResponse {
  track_id: string;
  action_type: string;
  diff: DiffItem[];
  old_path: string;
  new_path: string;
  path_changed: boolean;
  has_lyrics_change: boolean;
  has_cover_change: boolean;
  proposal: SongEditProposalPayload;
}

export interface AutoEnrichCandidate {
  label: string;
  confidence_pct: number;
  title: string;
  artist: string;
  album: string;
  year: number | null;
  track_number: number | null;
  recording_mbid?: string;
  release_mbid?: string;
}

export interface AutoEnrichResponse {
  candidates: AutoEnrichCandidate[];
  best: {
    title: string;
    artist: string;
    album: string;
    year: number | null;
    track_number: number | null;
    recording_mbid?: string;
    release_mbid?: string;
  };
  fetched_lyrics: string | null;
}

