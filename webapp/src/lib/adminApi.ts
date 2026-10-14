import { apiFetch } from './auth';
import { AdminUser, AdminUsersResponse, PendingAccessRequest, PendingAccessRequestsResponse } from '../types/admin';

/**
 * Fetch registered users with optional role and search query filters.
 */
export async function fetchAdminUsers(role?: string, query?: string): Promise<AdminUser[]> {
  const params = new URLSearchParams();
  if (role && role !== 'ALL') params.set('role', role);
  if (query && query.trim()) params.set('query', query.trim());

  const url = `/api/v1/app/admin/users${params.toString() ? `?${params.toString()}` : ''}`;
  const resp = await apiFetch(url);
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || `Failed to fetch users (HTTP ${resp.status})`);
  }
  const data: AdminUsersResponse = await resp.json();
  return data.users || [];
}

/**
 * Update a user's role tier (GUEST, UPLOADER, MODERATOR, ADMIN, OWNER).
 */
export async function updateUserRole(userId: number, role: string, reason?: string): Promise<AdminUser> {
  const resp = await apiFetch(`/api/v1/app/admin/users/${userId}/role`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role, reason: reason || 'Mini App role update' }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || `Failed to update user role (HTTP ${resp.status})`);
  }
  const data = await resp.json();
  return data.user;
}

/**
 * Block or unblock a user account.
 */
export async function updateUserBlockStatus(
  userId: number,
  isBlocked: boolean,
  reason?: string
): Promise<AdminUser> {
  const resp = await apiFetch(`/api/v1/app/admin/users/${userId}/block`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ is_blocked: isBlocked, reason: reason || 'Mini App block update' }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || `Failed to update block status (HTTP ${resp.status})`);
  }
  const data = await resp.json();
  return data.user;
}

/**
 * Fetch all pending access requests awaiting review.
 */
export async function fetchPendingAccessRequests(): Promise<PendingAccessRequest[]> {
  const resp = await apiFetch('/api/v1/app/admin/access-requests');
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || `Failed to fetch access requests (HTTP ${resp.status})`);
  }
  const data: PendingAccessRequestsResponse = await resp.json();
  return data.requests || [];
}

/**
 * Resolve a pending access request by approving (promoting to Uploader) or denying.
 */
export async function resolveAccessRequest(
  requestId: number,
  action: 'approve' | 'deny'
): Promise<{ request_id: number; status: string; user?: AdminUser }> {
  const resp = await apiFetch(`/api/v1/app/admin/access-requests/${requestId}/resolve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || `Failed to resolve access request (HTTP ${resp.status})`);
  }
  return resp.json();
}

