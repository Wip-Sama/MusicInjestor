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

  const loadData = useCallback(async (isRefresh = false) => {
    if (!isAuthorized) {
      setLoading(false);
      return;
    }
    if (isRefresh) setRefreshing(true);
    else setLoading(true);

    try {
      const [usersData, requestsData] = await Promise.all([
        fetchAdminUsers(roleFilter, searchQuery),
        fetchPendingAccessRequests(),
      ]);
      setUsers(usersData);
      setRequests(requestsData);
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
