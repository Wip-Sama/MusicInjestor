import { apiFetch } from './auth';
import { TrackItem } from '../types/music';

export interface ProviderCapabilitiesSummary {
  provider_name: string;
  version?: string;
  supported_sources: string[];
  streaming: boolean;
  provisioning?: boolean;
}

export interface ProviderServerInfo {
  id: string;
  name: string;
  base_url: string;
  priority: number;
  is_enabled: boolean;
  allow_streaming?: boolean;
  allow_archiving?: boolean;
  min_archival_quality?: string | null;
  supports_streaming?: boolean;
  supports_archiving?: boolean;
  last_health_status: string | null;
  last_health_checked_at: string | null;
  capabilities: ProviderCapabilitiesSummary | null;
}

export interface SongRequestItem {
  id: number;
  telegram_id?: number;
  request_type: 'TRACK' | 'ALBUM';
  title: string;
  artist: string;
  album: string;
  identifiers?: Record<string, string>;
  status: 'PENDING_APPROVAL' | 'APPROVED' | 'REJECTED' | 'PROVISIONING' | 'COMPLETED' | 'FAILED';
  provider_id?: string | null;
  job_id?: string | null;
  error_message?: string | null;
  target_quality?: string | null;
  created_at: string;
  library_track_id?: string | null;
}

export interface SubmitSongRequestPayload {
  request_type: 'TRACK' | 'ALBUM';
  track_title: string;
  artist_name: string;
  album_name?: string;
  identifiers?: Record<string, string>;
  target_quality?: string;
}

export interface ProvidersStatusResponse {
  has_providers: boolean;
  has_streaming_providers?: boolean;
  has_archival_providers?: boolean;
  enabled_count: number;
}

export async function fetchProvidersStatus(): Promise<ProvidersStatusResponse> {
  const resp = await apiFetch('/api/v1/app/providers/status');
  if (!resp.ok) {
    return {
      has_providers: false,
      has_streaming_providers: false,
      has_archival_providers: false,
      enabled_count: 0,
    };
  }
  return resp.json();
}

export interface ResolveProviderTrackResponse {
  matched: boolean;
  provider_id?: string;
  provider_name?: string;
  provider_track_id?: string;
  matched_source?: string;
  best_quality?: string | null;
  cover_url?: string | null;
  metadata?: {
    title?: string;
    artist?: string;
    album?: string;
    duration_seconds?: number | null;
    isrc?: string | null;
  } | null;
  stream_url?: string | null;
}

export async function resolveProviderTrack(payload: {
  title: string;
  artist: string;
  album?: string;
  identifiers?: Record<string, string>;
  purpose?: 'stream' | 'archive' | 'any';
}): Promise<ResolveProviderTrackResponse> {
  const resp = await apiFetch('/api/v1/app/providers/resolve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ purpose: 'stream', ...payload }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || `Failed to resolve track (HTTP ${resp.status})`);
  }
  return resp.json();
}

export async function fetchAdminProviders(): Promise<ProviderServerInfo[]> {
  const resp = await apiFetch('/api/v1/app/admin/providers');
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || `Failed to load providers (HTTP ${resp.status})`);
  }
  return resp.json();
}

export async function saveAdminProvider(payload: {
  id?: string;
  name: string;
  base_url: string;
  api_token?: string;
  priority?: number;
  is_enabled?: boolean;
  role?: 'streaming' | 'archival' | 'both';
  allow_streaming?: boolean;
  allow_archiving?: boolean;
  min_archival_quality?: string | null;
}): Promise<{ success: boolean; id: string; health: string | null }> {
  const resp = await apiFetch('/api/v1/app/admin/providers', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || `Failed to save provider (HTTP ${resp.status})`);
  }
  return resp.json();
}

export async function testAdminProvider(providerId: string): Promise<{
  success: boolean;
  status: string | null;
}> {
  const resp = await apiFetch(`/api/v1/app/admin/providers/${encodeURIComponent(providerId)}/test`, {
    method: 'POST',
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || `Provider test failed (HTTP ${resp.status})`);
  }
  return resp.json();
}

export async function toggleAdminProvider(providerId: string): Promise<{
  success: boolean;
  is_enabled: boolean;
}> {
  const resp = await apiFetch(`/api/v1/app/admin/providers/${encodeURIComponent(providerId)}/toggle`, {
    method: 'POST',
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || `Failed to toggle provider (HTTP ${resp.status})`);
  }
  return resp.json();
}

export async function deleteAdminProvider(providerId: string): Promise<void> {
  const resp = await apiFetch(`/api/v1/app/admin/providers/${encodeURIComponent(providerId)}`, {
    method: 'DELETE',
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || `Failed to delete provider (HTTP ${resp.status})`);
  }
}

export async function submitSongRequest(payload: SubmitSongRequestPayload): Promise<{
  success: boolean;
  request_id: number;
  status: string;
  is_auto_approved: boolean;
}> {
  const resp = await apiFetch('/api/v1/app/requests', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || `Failed to submit song request (HTTP ${resp.status})`);
  }
  return resp.json();
}

export async function fetchMySongRequests(): Promise<SongRequestItem[]> {
  const resp = await apiFetch('/api/v1/app/requests/me');
  if (!resp.ok) return [];
  return resp.json();
}

export async function fetchPendingSongRequests(): Promise<SongRequestItem[]> {
  const resp = await apiFetch('/api/v1/app/requests/pending');
  if (!resp.ok) return [];
  return resp.json();
}

export async function resolveSongRequest(
  requestId: number,
  approved: boolean,
  rejectionReason?: string
): Promise<{ success: boolean; status: string }> {
  const resp = await apiFetch(`/api/v1/app/requests/${requestId}/resolve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approved, rejection_reason: rejectionReason || '' }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || `Failed to resolve request (HTTP ${resp.status})`);
  }
  return resp.json();
}

export async function resolveStreamableTrack(params: {
  title: string;
  artist: string;
  album?: string;
  trackNumber?: number;
  durationSeconds?: number;
  year?: number | null;
  coverUrl?: string;
  identifiers?: Record<string, string>;
}): Promise<TrackItem | null> {
  const res = await resolveProviderTrack({
    title: params.title,
    artist: params.artist,
    album: params.album,
    identifiers: params.identifiers,
    purpose: 'stream',
  });
  if (!res.matched || !res.stream_url || !res.provider_id || !res.provider_track_id) {
    return null;
  }
  return {
    id: `provider:${res.provider_id}:${res.provider_track_id}`,
    title: params.title || res.metadata?.title || 'Unknown Title',
    artist: params.artist || res.metadata?.artist || 'Unknown Artist',
    album: params.album || res.metadata?.album || '',
    year: params.year ?? null,
    track_number: params.trackNumber ?? 1,
    duration_seconds: params.durationSeconds || res.metadata?.duration_seconds || 0,
    has_synced_lyrics: false,
    format: res.best_quality || 'stream',
    audio_url: res.stream_url,
    cover_url: params.coverUrl || res.cover_url || '',
    source_type: 'provider_stream',
    identifiers: {
      ...(res.metadata?.isrc ? { isrc: res.metadata.isrc } : {}),
      ...(params.identifiers || {}),
    },
    provider_name: res.provider_name || undefined,
  };
}

export interface StreamEnrichmentResponse {
  found: boolean;
  title: string | null;
  artist: string | null;
  album: string | null;
  cover_url: string | null;
  duration_seconds: number | null;
  track_number: number | null;
  deezer_id: string | null;
  external_url: string | null;
  has_synced: boolean;
  synced_lyrics: string | null;
  plain_lyrics: string | null;
}

export async function fetchStreamEnrichment(params: {
  title: string;
  artist: string;
  album?: string;
  durationSeconds?: number;
}): Promise<StreamEnrichmentResponse | null> {
  const qs = new URLSearchParams();
  if (params.title) qs.set('title', params.title);
  if (params.artist) qs.set('artist', params.artist);
  if (params.album) qs.set('album', params.album);
  if (params.durationSeconds && params.durationSeconds > 0) {
    qs.set('duration', String(Math.round(params.durationSeconds)));
  }
  const resp = await apiFetch(`/api/v1/app/streams/enrichment?${qs.toString()}`);
  if (!resp.ok) {
    return null;
  }
  return resp.json();
}
