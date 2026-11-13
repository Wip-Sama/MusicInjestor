import { apiFetch } from './auth';

export interface ProviderCapabilitiesSummary {
  provider_name: string;
  version?: string;
  supported_sources: string[];
  streaming: boolean;
}

export interface ProviderServerInfo {
  id: string;
  name: string;
  base_url: string;
  priority: number;
  is_enabled: boolean;
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

export async function fetchProvidersStatus(): Promise<{
  has_providers: boolean;
  enabled_count: number;
}> {
  const resp = await apiFetch('/api/v1/app/providers/status');
  if (!resp.ok) {
    return { has_providers: false, enabled_count: 0 };
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
