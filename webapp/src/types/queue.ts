export type QueueJobState =
  | 'QUEUED'
  | 'PAUSED'
  | 'PENDING'
  | 'PROCESSING'
  | 'EXTRACTING'
  | 'FINGERPRINTING'
  | 'ENRICHING_METADATA'
  | 'AWAITING_CONFIRMATION'
  | 'AWAITING_INPUT'
  | 'FETCHING_LYRICS'
  | 'ORGANIZING'
  | 'COMPLETED'
  | 'DUPLICATE_REJECTED'
  | 'CANCELLED'
  | 'FAILED';

export interface CandidateItem {
  index: number;
  confidence: number;
  release_type: string;
  disambiguation: string;
  title: string;
  artist: string;
  album: string;
  year: number | null;
  track_number: number | null;
  release_mbid: string | null;
  recording_mbid: string | null;
}

export interface SelectedMetadata {
  title: string;
  artist: string;
  album: string;
  year: number | null;
  track_number: number | null;
  disc_number?: number | null;
  genre?: string | null;
  duration_seconds?: number | null;
  recording_mbid?: string | null;
  release_mbid?: string | null;
}

export interface QueueJob {
  job_id: string;
  state: QueueJobState;
  source_type: string;
  original_filename: string;
  file_size_bytes: number;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
  confidence_score: number | null;
  matched_release_mbid: string | null;
  final_path: string | null;
  user_id: number | null;
  queue_position: number;
  total_tracks: number;
  processed_tracks: number;
  lyrics_matched_count: number;
  selected_metadata: SelectedMetadata | null;
  candidates: CandidateItem[];
}

export interface QueueResponse {
  concurrency_limit: number;
  active_count: number;
  queued_count: number;
  paused_count: number;
  can_view_global: boolean;
  scope: 'mine' | 'global';
  jobs: QueueJob[];
}

export interface QueueEvent {
  type:
    | 'job_enqueued'
    | 'stage_changed'
    | 'confirmation_required'
    | 'job_completed'
    | 'job_failed'
    | 'job_state_changed'
    | 'connected';
  job_id?: string;
  user_id?: number;
  state?: string;
  job?: QueueJob;
}

