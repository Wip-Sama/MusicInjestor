import React, { useEffect, useState, useCallback } from 'react';
import {
  Shield,
  ShieldAlert,
  Search,
  Check,
  X,
  UserCheck,
  Ban,
  Clock,
  RefreshCw,
  FileMusic,
  AlertTriangle,
  Server,
  Plus,
  Trash2,
  Power,
  Download,
  Activity,
} from 'lucide-react';
import { Card, Badge, Spinner } from '@telegram-apps/telegram-ui';
import { UserProfile, canManageUsers } from '../lib/auth';
import {
  AdminUser,
  PendingAccessRequest,
} from '../types/admin';
import {
  fetchAdminUsers,
  updateUserRole,
  updateUserBlockStatus,
  fetchPendingAccessRequests,
  resolveAccessRequest,
} from '../lib/adminApi';
import {
  ProviderServerInfo,
  SongRequestItem,
  fetchAdminProviders,
  saveAdminProvider,
  testAdminProvider,
  toggleAdminProvider,
  deleteAdminProvider,
  fetchPendingSongRequests,
  resolveSongRequest,
} from '../lib/providerApi';

interface GovernanceViewProps {
  profile: UserProfile | null;
}

const ROLES = ['GUEST', 'LISTENER', 'UPLOADER', 'MODERATOR', 'ADMIN', 'OWNER'] as const;

export const GovernanceView: React.FC<GovernanceViewProps> = ({ profile }) => {
  const isAuthorized = canManageUsers(profile);

  const [users, setUsers] = useState<AdminUser[]>([]);
  const [requests, setRequests] = useState<PendingAccessRequest[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [roleFilter, setRoleFilter] = useState<string>('ALL');
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionSuccess, setActionSuccess] = useState<string | null>(null);
  const [operatingId, setOperatingId] = useState<number | null>(null);
  const [providers, setProviders] = useState<ProviderServerInfo[]>([]);
  const [songRequests, setSongRequests] = useState<SongRequestItem[]>([]);
  const [showAddProvider, setShowAddProvider] = useState<boolean>(false);
  const [newProvName, setNewProvName] = useState<string>('');
  const [newProvUrl, setNewProvUrl] = useState<string>('');
  const [newProvToken, setNewProvToken] = useState<string>('');
  const [newProvPriority, setNewProvPriority] = useState<number>(100);
  const [newProvRole, setNewProvRole] = useState<'both' | 'streaming' | 'archival'>('both');
  const [newProvMinQuality, setNewProvMinQuality] = useState<string>('');
  const [operatingProviderId, setOperatingProviderId] = useState<string | null>(null);
  const [operatingSongReqId, setOperatingSongReqId] = useState<number | null>(null);

  const loadData = useCallback(async (isRefresh = false) => {
    if (!isAuthorized) {
      setLoading(false);
      return;
    }
    if (isRefresh) setRefreshing(true);
    else setLoading(true);

    try {
      const [usersData, requestsData, providersData, songReqsData] = await Promise.all([
        fetchAdminUsers(roleFilter, searchQuery),
        fetchPendingAccessRequests(),
        fetchAdminProviders().catch(() => [] as ProviderServerInfo[]),
        fetchPendingSongRequests().catch(() => [] as SongRequestItem[]),
      ]);
      setUsers(usersData);
      setRequests(requestsData);
      setProviders(providersData);
      setSongRequests(songReqsData);
      setActionError(null);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to load governance data';
      setActionError(msg);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [isAuthorized, roleFilter, searchQuery]);

  useEffect(() => {
    const handler = setTimeout(() => {
      loadData();
    }, 250);
    return () => clearTimeout(handler);
  }, [loadData]);

  // Handle role change
  const handleAddProvider = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newProvName.trim() || !newProvUrl.trim()) return;
    setOperatingProviderId('new');
    setActionError(null);
    try {
      await saveAdminProvider({
        name: newProvName.trim(),
        base_url: newProvUrl.trim(),
        api_token: newProvToken.trim() || undefined,
        priority: Number.isFinite(newProvPriority) ? newProvPriority : 100,
        is_enabled: true,
        role: newProvRole,
        min_archival_quality:
          newProvRole === 'streaming' ? null : newProvMinQuality || null,
      });
      setNewProvName('');
      setNewProvUrl('');
      setNewProvToken('');
      setNewProvPriority(100);
      setNewProvRole('both');
      setNewProvMinQuality('');
      setShowAddProvider(false);
      setActionSuccess('Provider saved and health-checked');
      setTimeout(() => setActionSuccess(null), 3000);
      const updated = await fetchAdminProviders();
      setProviders(updated);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Failed to add provider');
    } finally {
      setOperatingProviderId(null);
    }
  };

  const handleTestProvider = async (provId: string) => {
    setOperatingProviderId(provId);
    setActionError(null);
    try {
      const res = await testAdminProvider(provId);
      setActionSuccess(`Health check: ${res.status || 'checked'}`);
      setTimeout(() => setActionSuccess(null), 3000);
      const updated = await fetchAdminProviders();
      setProviders(updated);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Health check failed');
    } finally {
      setOperatingProviderId(null);
    }
  };

  const handleToggleProvider = async (provId: string) => {
    setOperatingProviderId(provId);
    setActionError(null);
    try {
      const res = await toggleAdminProvider(provId);
      setProviders((prev) =>
        prev.map((p) => (p.id === provId ? { ...p, is_enabled: res.is_enabled } : p))
      );
      setActionSuccess(res.is_enabled ? 'Provider enabled' : 'Provider disabled');
      setTimeout(() => setActionSuccess(null), 2500);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Failed to toggle provider');
    } finally {
      setOperatingProviderId(null);
    }
  };

  const handleDeleteProvider = async (provId: string) => {
    setOperatingProviderId(provId);
    setActionError(null);
    try {
      await deleteAdminProvider(provId);
      setProviders((prev) => prev.filter((p) => p.id !== provId));
      setActionSuccess('Provider removed');
      setTimeout(() => setActionSuccess(null), 2500);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Failed to remove provider');
    } finally {
      setOperatingProviderId(null);
    }
  };

  const handleResolveSongRequest = async (reqId: number, approved: boolean) => {
    setOperatingSongReqId(reqId);
    setActionError(null);
    try {
      await resolveSongRequest(reqId, approved);
      setSongRequests((prev) => prev.filter((r) => r.id !== reqId));
      setActionSuccess(approved ? 'Request approved & provisioning started' : 'Request rejected');
      setTimeout(() => setActionSuccess(null), 3000);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Failed to resolve request');
    } finally {
      setOperatingSongReqId(null);
    }
  };

  const handleRoleChange = async (targetUser: AdminUser, newRole: string) => {
    if (targetUser.role === newRole) return;
    setOperatingId(targetUser.telegram_id);
    setActionError(null);
    try {
      const updated = await updateUserRole(
        targetUser.telegram_id,
        newRole,
        `Role updated to ${newRole} via Mini App Governance`
      );
      setUsers((prev) =>
        prev.map((u) => (u.telegram_id === updated.telegram_id ? updated : u))
      );
      setActionSuccess(`Updated ${targetUser.display_name} to ${newRole}`);
      setTimeout(() => setActionSuccess(null), 3000);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to update role';
      setActionError(msg);
    } finally {
      setOperatingId(null);
    }
  };

  // Handle block/unblock toggle
  const handleToggleBlock = async (targetUser: AdminUser) => {
    const nextBlocked = !targetUser.is_blocked;
    setOperatingId(targetUser.telegram_id);
    setActionError(null);
    try {
      const updated = await updateUserBlockStatus(
        targetUser.telegram_id,
        nextBlocked,
        nextBlocked ? 'Blocked via Mini App' : 'Unblocked via Mini App'
      );
      setUsers((prev) =>
        prev.map((u) => (u.telegram_id === updated.telegram_id ? updated : u))
      );
      setActionSuccess(
        `${nextBlocked ? 'Blocked' : 'Unblocked'} ${targetUser.display_name}`
      );
      setTimeout(() => setActionSuccess(null), 3000);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to update block status';
      setActionError(msg);
    } finally {
      setOperatingId(null);
    }
  };

  // Handle access request approval/rejection
  const handleResolveRequest = async (requestId: number, action: 'approve' | 'deny') => {
    setOperatingId(requestId);
    setActionError(null);
    try {
      const res = await resolveAccessRequest(requestId, action);
      setRequests((prev) => prev.filter((r) => r.id !== requestId));
      if (res.user) {
        setUsers((prev) => {
          const exists = prev.some((u) => u.telegram_id === res.user?.telegram_id);
          if (exists) {
            return prev.map((u) => (u.telegram_id === res.user?.telegram_id ? res.user! : u));
          }
          return [res.user!, ...prev];
        });
      }
      setActionSuccess(
        action === 'approve'
          ? 'Access request approved! User promoted to Uploader.'
          : 'Access request rejected.'
      );
      setTimeout(() => setActionSuccess(null), 3000);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to resolve request';
      setActionError(msg);
    } finally {
      setOperatingId(null);
    }
  };

  // 1. Feature Lock Screen for unauthorized users
  if (!isAuthorized) {
    return (
      <Card
        style={{
          padding: '32px 20px',
          textAlign: 'center',
          backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
          borderRadius: '14px',
          border: '1px solid rgba(255, 69, 58, 0.3)',
        }}
      >
        <ShieldAlert size={42} color="#ff453a" style={{ margin: '0 auto 12px auto' }} />
        <div style={{ fontSize: '17px', fontWeight: 700, color: 'var(--tg-theme-text-color, #fff)' }}>
          Access Restricted
        </div>
        <div
          style={{
            fontSize: '13px',
            color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
            marginTop: '8px',
            maxWidth: '380px',
            margin: '8px auto 0 auto',
            lineHeight: 1.4,
          }}
        >
          The Governance &amp; User Administration console is strictly restricted to <strong>ADMIN</strong> and <strong>OWNER</strong> roles. Your account role is <code>{profile?.role || 'GUEST'}</code>.
        </div>
      </Card>
    );
  }

  const getRoleBadgeMode = (role: string) => {
    switch (role) {
      case 'OWNER':
        return 'critical';
      case 'ADMIN':
        return 'primary';
      case 'MODERATOR':
        return 'secondary';
      case 'UPLOADER':
        return 'primary';
      case 'LISTENER':
        return 'secondary';
      default:
        return 'gray';
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      {/* Header card with telemetry */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
          borderRadius: '12px',
          padding: '12px 14px',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <Shield size={22} color="var(--tg-theme-button-color, #0a84ff)" />
          <div>
            <div style={{ fontSize: '15px', fontWeight: 700 }}>User Governance &amp; RBAC</div>
            <div style={{ fontSize: '12px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
              {users.length} registered accounts • {requests.length} pending review
            </div>
          </div>
        </div>

        <button
          onClick={() => loadData(true)}
          disabled={refreshing}
          style={{
            background: 'none',
            border: 'none',
            color: 'var(--tg-theme-button-color, #0a84ff)',
            cursor: 'pointer',
            padding: '4px',
            display: 'flex',
            alignItems: 'center',
            gap: '4px',
            fontSize: '12px',
          }}
        >
          <RefreshCw size={14} className={refreshing ? 'spin' : ''} />
          <span>Refresh</span>
        </button>
      </div>

      {/* Toast Feedback Messages */}
      {actionSuccess && (
        <div
          style={{
            backgroundColor: 'rgba(48, 209, 88, 0.15)',
            border: '1px solid rgba(48, 209, 88, 0.4)',
            color: '#30d158',
            borderRadius: '10px',
            padding: '10px 14px',
            fontSize: '13px',
            fontWeight: 600,
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
          }}
        >
          <Check size={16} />
          <span>{actionSuccess}</span>
        </div>
      )}

      {actionError && (
        <div
          style={{
            backgroundColor: 'rgba(255, 69, 58, 0.15)',
            border: '1px solid rgba(255, 69, 58, 0.4)',
            color: '#ff453a',
            borderRadius: '10px',
            padding: '10px 14px',
            fontSize: '13px',
            fontWeight: 600,
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
          }}
        >
          <AlertTriangle size={16} />
          <span>{actionError}</span>
        </div>
      )}

      {/* Section 0: External Music Providers */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', fontWeight: 700, color: '#bf5af2' }}>
            <Server size={15} />
            <span>External Music Providers ({providers.length})</span>
          </div>
          <button
            onClick={() => setShowAddProvider((v) => !v)}
            style={{
              backgroundColor: 'rgba(191, 90, 242, 0.16)',
              color: '#bf5af2',
              border: 'none',
              borderRadius: '6px',
              padding: '4px 10px',
              fontSize: '11px',
              fontWeight: 600,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
            }}
          >
            <Plus size={12} />
            <span>{showAddProvider ? 'Cancel' : 'Add Provider'}</span>
          </button>
        </div>

        {showAddProvider && (
          <Card
            style={{
              padding: '12px',
              backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
              borderRadius: '10px',
              border: '1px solid rgba(191, 90, 242, 0.35)',
            }}
          >
            <form onSubmit={handleAddProvider} style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              <input
                type="text"
                placeholder="Provider Name (e.g. Default Provider)"
                value={newProvName}
                onChange={(e) => setNewProvName(e.target.value)}
                required
                style={{
                  padding: '8px 10px',
                  borderRadius: '8px',
                  border: '1px solid rgba(255,255,255,0.12)',
                  backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
                  color: 'var(--tg-theme-text-color, #ffffff)',
                  fontSize: '12px',
                }}
              />
              <input
                type="url"
                placeholder="Base URL (e.g. http://127.0.0.1:8085)"
                value={newProvUrl}
                onChange={(e) => setNewProvUrl(e.target.value)}
                required
                style={{
                  padding: '8px 10px',
                  borderRadius: '8px',
                  border: '1px solid rgba(255,255,255,0.12)',
                  backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
                  color: 'var(--tg-theme-text-color, #ffffff)',
                  fontSize: '12px',
                }}
              />
              <div style={{ display: 'flex', gap: '8px' }}>
                <input
                  type="password"
                  placeholder="Bearer Token (optional)"
                  value={newProvToken}
                  onChange={(e) => setNewProvToken(e.target.value)}
                  style={{
                    flex: 1,
                    padding: '8px 10px',
                    borderRadius: '8px',
                    border: '1px solid rgba(255,255,255,0.12)',
                    backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
                    color: 'var(--tg-theme-text-color, #ffffff)',
                    fontSize: '12px',
                  }}
                />
                <input
                  type="number"
                  placeholder="Priority"
                  value={newProvPriority}
                  onChange={(e) => setNewProvPriority(parseInt(e.target.value || '100', 10))}
                  style={{
                    width: '84px',
                    padding: '8px 10px',
                    borderRadius: '8px',
                    border: '1px solid rgba(255,255,255,0.12)',
                    backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
                    color: 'var(--tg-theme-text-color, #ffffff)',
                    fontSize: '12px',
                  }}
                />
              </div>
              <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                <select
                  value={newProvRole}
                  onChange={(e) =>
                    setNewProvRole(e.target.value as 'both' | 'streaming' | 'archival')
                  }
                  aria-label="Provider Role"
                  style={{
                    flex: 1,
                    minWidth: '150px',
                    padding: '8px 10px',
                    borderRadius: '8px',
                    border: '1px solid rgba(255,255,255,0.12)',
                    backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
                    color: 'var(--tg-theme-text-color, #ffffff)',
                    fontSize: '12px',
                  }}
                >
                  <option value="both">Role: Auto / Dual (From Capabilities)</option>
                  <option value="streaming">Override: Streaming Only (Fast / Lossy)</option>
                  <option value="archival">Override: Archival Only (FLAC / Lossless)</option>
                </select>
                {newProvRole !== 'streaming' && (
                  <select
                    value={newProvMinQuality}
                    onChange={(e) => setNewProvMinQuality(e.target.value)}
                    aria-label="Minimum Archival Quality"
                    style={{
                      flex: 1,
                      minWidth: '140px',
                      padding: '8px 10px',
                      borderRadius: '8px',
                      border: '1px solid rgba(255,255,255,0.12)',
                      backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
                      color: 'var(--tg-theme-text-color, #ffffff)',
                      fontSize: '12px',
                    }}
                  >
                    <option value="">Min Archive: Global Default</option>
                    <option value="lossless">Min Archive: Lossless (FLAC)</option>
                    <option value="hires">Min Archive: Hi-Res (24-bit+)</option>
                    <option value="lossy_high">Min Archive: Lossy High (256k+)</option>
                    <option value="any">Min Archive: Any Quality</option>
                  </select>
                )}
              </div>
              <button
                type="submit"
                disabled={operatingProviderId === 'new'}
                style={{
                  backgroundColor: '#bf5af2',
                  color: '#fff',
                  border: 'none',
                  borderRadius: '8px',
                  padding: '8px 12px',
                  fontSize: '12px',
                  fontWeight: 700,
                  cursor: 'pointer',
                }}
              >
                {operatingProviderId === 'new' ? 'Saving...' : 'Save & Test Provider'}
              </button>
            </form>
          </Card>
        )}

        {providers.length === 0 ? (
          <Card
            style={{
              padding: '12px 14px',
              backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
              borderRadius: '10px',
              fontSize: '12px',
              color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
            }}
          >
            No external music providers configured yet. Add one above or configure{' '}
            <code>providers:</code> in <code>config.yaml</code>.
          </Card>
        ) : (
          providers.map((prov) => {
            const isHealthy = prov.last_health_status === 'ok' || prov.last_health_status === 'healthy';
            const canStream =
              prov.supports_streaming ??
              (prov.allow_streaming !== false &&
                (prov.capabilities ? prov.capabilities.streaming : true));
            const canArchive =
              prov.supports_archiving ??
              (prov.allow_archiving !== false &&
                (prov.capabilities ? prov.capabilities.provisioning !== false : true));
            let roleLabel = 'No Active Role';
            let roleBg = 'rgba(142, 142, 147, 0.16)';
            let roleColor = '#8e8e93';
            if (canStream && canArchive) {
              roleLabel = prov.min_archival_quality
                ? `Stream + Archive (≥${prov.min_archival_quality})`
                : 'Stream + Archive';
              roleBg = 'rgba(191, 90, 242, 0.16)';
              roleColor = '#bf5af2';
            } else if (canStream) {
              roleLabel = 'Streaming Only';
              roleBg = 'rgba(10, 132, 255, 0.16)';
              roleColor = '#0a84ff';
            } else if (canArchive) {
              roleLabel = prov.min_archival_quality
                ? `Archiver (≥${prov.min_archival_quality})`
                : 'FLAC Archiver';
              roleBg = 'rgba(94, 92, 230, 0.18)';
              roleColor = '#9b99ff';
            }
            return (
              <Card
                key={prov.id}
                style={{
                  padding: '12px 14px',
                  backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                  borderRadius: '10px',
                  border: prov.is_enabled
                    ? '1px solid rgba(191, 90, 242, 0.28)'
                    : '1px solid rgba(255,255,255,0.06)',
                  display: 'flex',
                  flexDirection: 'column',
                  gap: '8px',
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: '13px', fontWeight: 700, display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
                      <span>{prov.name}</span>
                      <span
                        style={{
                          fontSize: '10px',
                          padding: '2px 6px',
                          borderRadius: '4px',
                          backgroundColor: isHealthy ? 'rgba(48, 209, 88, 0.16)' : 'rgba(255, 159, 10, 0.16)',
                          color: isHealthy ? '#30d158' : '#ff9f0a',
                        }}
                      >
                        {prov.last_health_status || 'unknown'}
                      </span>
                      <span
                        style={{
                          fontSize: '10px',
                          padding: '2px 6px',
                          borderRadius: '4px',
                          backgroundColor: roleBg,
                          color: roleColor,
                        }}
                      >
                        {roleLabel}
                      </span>
                      {!prov.is_enabled && (
                        <span
                          style={{
                            fontSize: '10px',
                            padding: '2px 6px',
                            borderRadius: '4px',
                            backgroundColor: 'rgba(255, 69, 58, 0.16)',
                            color: '#ff453a',
                          }}
                        >
                          disabled
                        </span>
                      )}
                    </div>
                    <div style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', wordBreak: 'break-all' }}>
                      {prov.base_url} • Priority {prov.priority}
                      {prov.capabilities?.supported_sources?.length
                        ? ` • ${prov.capabilities.supported_sources.join(', ')}`
                        : ''}
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: '6px' }}>
                    <button
                      disabled={operatingProviderId === prov.id}
                      onClick={() => handleTestProvider(prov.id)}
                      style={{
                        backgroundColor: 'rgba(10, 132, 255, 0.15)',
                        color: '#0a84ff',
                        border: 'none',
                        borderRadius: '6px',
                        padding: '5px 8px',
                        fontSize: '11px',
                        fontWeight: 600,
                        cursor: 'pointer',
                        display: 'flex',
                        alignItems: 'center',
                        gap: '4px',
                      }}
                    >
                      <Activity size={12} />
                      <span>Test</span>
                    </button>
                    <button
                      disabled={operatingProviderId === prov.id}
                      onClick={() => handleToggleProvider(prov.id)}
                      style={{
                        backgroundColor: prov.is_enabled
                          ? 'rgba(255, 159, 10, 0.15)'
                          : 'rgba(48, 209, 88, 0.15)',
                        color: prov.is_enabled ? '#ff9f0a' : '#30d158',
                        border: 'none',
                        borderRadius: '6px',
                        padding: '5px 8px',
                        fontSize: '11px',
                        fontWeight: 600,
                        cursor: 'pointer',
                        display: 'flex',
                        alignItems: 'center',
                        gap: '4px',
                      }}
                    >
                      <Power size={12} />
                      <span>{prov.is_enabled ? 'Disable' : 'Enable'}</span>
                    </button>
                    <button
                      disabled={operatingProviderId === prov.id}
                      onClick={() => handleDeleteProvider(prov.id)}
                      style={{
                        backgroundColor: 'rgba(255, 69, 58, 0.15)',
                        color: '#ff453a',
                        border: 'none',
                        borderRadius: '6px',
                        padding: '5px 8px',
                        fontSize: '11px',
                        fontWeight: 600,
                        cursor: 'pointer',
                        display: 'flex',
                        alignItems: 'center',
                      }}
                      title="Delete provider"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                </div>
              </Card>
            );
          })
        )}
      </div>

      {/* Section 0b: Pending Song Requests */}
      {songRequests.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', fontWeight: 700, color: '#0a84ff' }}>
            <Download size={15} />
            <span>Pending Song Requests ({songRequests.length})</span>
          </div>

          {songRequests.map((sreq) => (
            <Card
              key={sreq.id}
              style={{
                padding: '12px 14px',
                backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                borderRadius: '10px',
                border: '1px solid rgba(10, 132, 255, 0.3)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: '10px',
              }}
            >
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: '13px', fontWeight: 600 }}>
                  {sreq.artist} — {sreq.title}
                </div>
                <div style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                  {sreq.request_type}{sreq.album ? ` • ${sreq.album}` : ''} • User {sreq.telegram_id}
                </div>
              </div>
              <div style={{ display: 'flex', gap: '6px', flexShrink: 0 }}>
                <button
                  disabled={operatingSongReqId === sreq.id}
                  onClick={() => handleResolveSongRequest(sreq.id, true)}
                  style={{
                    backgroundColor: 'rgba(48, 209, 88, 0.2)',
                    color: '#30d158',
                    border: 'none',
                    borderRadius: '6px',
                    padding: '6px 10px',
                    fontSize: '12px',
                    fontWeight: 600,
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '4px',
                  }}
                >
                  <Check size={14} />
                  <span>Approve</span>
                </button>
                <button
                  disabled={operatingSongReqId === sreq.id}
                  onClick={() => handleResolveSongRequest(sreq.id, false)}
                  style={{
                    backgroundColor: 'rgba(255, 69, 58, 0.2)',
                    color: '#ff453a',
                    border: 'none',
                    borderRadius: '6px',
                    padding: '6px 10px',
                    fontSize: '12px',
                    fontWeight: 600,
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '4px',
                  }}
                >
                  <X size={14} />
                  <span>Reject</span>
                </button>
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* Section 1: Pending Access Requests */}
      {requests.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', fontWeight: 700, color: '#ff9f0a' }}>
            <Clock size={15} />
            <span>Pending Access Requests ({requests.length})</span>
          </div>

          {requests.map((req) => (
            <Card
              key={req.id}
              style={{
                padding: '12px 14px',
                backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                borderRadius: '10px',
                border: '1px solid rgba(255, 159, 10, 0.3)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: '10px',
              }}
            >
              <div>
                <div style={{ fontSize: '14px', fontWeight: 600 }}>{req.display_name}</div>
                <div style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                  {req.username ? `@${req.username} • ` : ''}ID: {req.telegram_id}
                </div>
              </div>

              <div style={{ display: 'flex', gap: '6px' }}>
                <button
                  disabled={operatingId === req.id}
                  onClick={() => handleResolveRequest(req.id, 'approve')}
                  style={{
                    backgroundColor: '#30d158',
                    color: '#fff',
                    border: 'none',
                    borderRadius: '6px',
                    padding: '6px 10px',
                    fontSize: '12px',
                    fontWeight: 600,
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '4px',
                  }}
                >
                  <Check size={14} />
                  <span>Approve</span>
                </button>
                <button
                  disabled={operatingId === req.id}
                  onClick={() => handleResolveRequest(req.id, 'deny')}
                  style={{
                    backgroundColor: 'rgba(255, 69, 58, 0.2)',
                    color: '#ff453a',
                    border: 'none',
                    borderRadius: '6px',
                    padding: '6px 10px',
                    fontSize: '12px',
                    fontWeight: 600,
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '4px',
                  }}
                >
                  <X size={14} />
                  <span>Deny</span>
                </button>
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* Section 2: Search & Filter Controls */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
            borderRadius: '10px',
            padding: '8px 12px',
            gap: '8px',
          }}
        >
          <Search size={16} color="var(--tg-theme-subtitle-text-color, #8e8e93)" />
          <input
            type="text"
            placeholder="Search by name, @username, or Telegram ID..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            style={{
              flex: 1,
              background: 'transparent',
              border: 'none',
              outline: 'none',
              color: 'var(--tg-theme-text-color, #fff)',
              fontSize: '13px',
            }}
          />
        </div>

        {/* Role filter pills */}
        <div style={{ display: 'flex', gap: '6px', overflowX: 'auto', paddingBottom: '4px' }}>
          {(['ALL', ...ROLES] as const).map((r) => (
            <button
              key={r}
              onClick={() => setRoleFilter(r)}
              style={{
                border: 'none',
                borderRadius: '16px',
                padding: '4px 10px',
                fontSize: '11px',
                fontWeight: 600,
                cursor: 'pointer',
                backgroundColor:
                  roleFilter === r ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                color: roleFilter === r ? '#fff' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                whiteSpace: 'nowrap',
              }}
            >
              {r}
            </button>
          ))}
        </div>
      </div>

      {/* Loading state */}
      {loading && (
        <div style={{ display: 'flex', justifyContent: 'center', padding: '40px 0' }}>
          <Spinner size="m" />
        </div>
      )}

      {/* Section 3: Users List */}
      {!loading && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          {users.length === 0 ? (
            <div
              style={{
                textAlign: 'center',
                padding: '40px 20px',
                color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                fontSize: '13px',
              }}
            >
              No user accounts found matching your query.
            </div>
          ) : (
            users.map((u) => {
              const isSelf = u.telegram_id === profile?.telegram_id;
              const isTargetOwner = u.role === 'OWNER';
              const isCallerOwner = profile?.role === 'OWNER' || profile?.is_owner;
              const cannotModify = isSelf || (isTargetOwner && !isCallerOwner);

              return (
                <Card
                  key={u.telegram_id}
                  style={{
                    padding: '12px 14px',
                    backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                    borderRadius: '10px',
                    border: u.is_blocked ? '1px solid rgba(255, 69, 58, 0.4)' : '1px solid transparent',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '10px',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                        <span style={{ fontSize: '14px', fontWeight: 600 }}>{u.display_name}</span>
                        {isSelf && (
                          <span
                            style={{
                              fontSize: '10px',
                              backgroundColor: 'rgba(10, 132, 255, 0.15)',
                              color: 'var(--tg-theme-button-color, #0a84ff)',
                              padding: '1px 5px',
                              borderRadius: '4px',
                              fontWeight: 700,
                            }}
                          >
                            YOU
                          </span>
                        )}
                      </div>
                      <div style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                        {u.username ? `@${u.username} • ` : ''}ID: <code>{u.telegram_id}</code>
                      </div>
                    </div>

                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                      <Badge mode={getRoleBadgeMode(u.role)} type="number">
                        {u.role}
                      </Badge>
                      {u.is_blocked && (
                        <span
                          style={{
                            fontSize: '10px',
                            backgroundColor: 'rgba(255, 69, 58, 0.2)',
                            color: '#ff453a',
                            padding: '2px 6px',
                            borderRadius: '4px',
                            fontWeight: 700,
                          }}
                        >
                          BLOCKED
                        </span>
                      )}
                    </div>
                  </div>

                  {/* Telemetry info row */}
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      fontSize: '11px',
                      color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                      paddingTop: '4px',
                      borderTop: '1px solid rgba(255, 255, 255, 0.05)',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                      <FileMusic size={12} />
                      <span>
                        Uploads: <strong>{u.uploads_count?.albums || 0}</strong> albums /{' '}
                        <strong>{u.uploads_count?.tracks || 0}</strong> tracks
                      </span>
                    </div>
                    {u.authentik_username && (
                      <span>SSO: <code>{u.authentik_username}</code></span>
                    )}
                  </div>

                  {/* Action Controls Row */}
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      gap: '8px',
                      paddingTop: '6px',
                    }}
                  >
                    {/* Role Select Dropdown */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                      <span style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                        Role:
                      </span>
                      <select
                        value={u.role}
                        disabled={cannotModify || operatingId === u.telegram_id}
                        onChange={(e) => handleRoleChange(u, e.target.value)}
                        style={{
                          backgroundColor: 'rgba(255, 255, 255, 0.08)',
                          color: cannotModify ? '#777' : 'var(--tg-theme-text-color, #fff)',
                          border: 'none',
                          borderRadius: '6px',
                          padding: '4px 8px',
                          fontSize: '12px',
                          fontWeight: 600,
                          cursor: cannotModify ? 'not-allowed' : 'pointer',
                        }}
                      >
                        {ROLES.map((r) => {
                          if (r === 'OWNER' && !isCallerOwner) return null;
                          return (
                            <option key={r} value={r} style={{ backgroundColor: '#2c2c2e', color: '#fff' }}>
                              {r}
                            </option>
                          );
                        })}
                      </select>
                    </div>

                    {/* Block / Unblock Button */}
                    <button
                      disabled={cannotModify || operatingId === u.telegram_id}
                      onClick={() => handleToggleBlock(u)}
                      style={{
                        backgroundColor: u.is_blocked
                          ? 'rgba(48, 209, 88, 0.15)'
                          : 'rgba(255, 69, 58, 0.15)',
                        color: u.is_blocked ? '#30d158' : '#ff453a',
                        border: 'none',
                        borderRadius: '6px',
                        padding: '4px 10px',
                        fontSize: '11px',
                        fontWeight: 600,
                        cursor: cannotModify ? 'not-allowed' : 'pointer',
                        display: 'flex',
                        alignItems: 'center',
                        gap: '4px',
                        opacity: cannotModify ? 0.5 : 1,
                      }}
                    >
                      {u.is_blocked ? (
                        <>
                          <UserCheck size={12} />
                          <span>Unblock</span>
                        </>
                      ) : (
                        <>
                          <Ban size={12} />
                          <span>Block</span>
                        </>
                      )}
                    </button>
                  </div>
                </Card>
              );
            })
          )}
        </div>
      )}
    </div>
  );
};
