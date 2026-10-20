import React, { useEffect, useState, useRef } from 'react';
import {
  AppRoot,
  Card,
  Headline,
  Subheadline,
  Caption,
  Badge,
  Spinner,
} from '@telegram-apps/telegram-ui';
import {
  Music,
  CheckCircle2,
  AlertCircle,
  Library,
  Radio,
  UserCheck,
  UploadCloud,
  Shield,
  ShieldAlert,
  Clock,
  SlidersHorizontal,
} from 'lucide-react';
import { AudioPlayerProvider, useAudioPlayer } from './context/AudioPlayerContext';
import { MiniPlayer } from './components/player/MiniPlayer';
import { FullscreenPlayer } from './components/player/FullscreenPlayer';
import { SyncedLyrics } from './components/player/SyncedLyrics';
import { QueueDrawer } from './components/player/QueueDrawer';
import { SettingsModal } from './components/settings/SettingsModal';
import { CatalogView } from './views/CatalogView';
import { RadarView } from './views/RadarView';
import { IngestQueueView } from './views/IngestQueueView';
import { GovernanceView } from './views/GovernanceView';
import {
  fetchCurrentProfile,
  UserProfile,
  canManageUsers,
  canViewCatalog,
  apiFetch,
} from './lib/auth';

type TabType = 'catalog' | 'radar' | 'ingest' | 'governance' | 'profile';

const AppContent: React.FC = () => {
  const { playTrack, setIsPlayerOpen } = useAudioPlayer();
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<TabType>('catalog');
  const [isSettingsOpen, setIsSettingsOpen] = useState<boolean>(false);
  const handledDeepLinkRef = useRef<boolean>(false);

  useEffect(() => {
    fetchCurrentProfile()
      .then((data) => {
        setProfile(data);
        setLoading(false);

        // Check deep-linking start_param once on launch
        if (!handledDeepLinkRef.current) {
          handledDeepLinkRef.current = true;
          const urlParams = new URLSearchParams(window.location.search);
          const startParam =
            data.start_param ||
            urlParams.get('startapp') ||
            urlParams.get('tgWebAppStartParam') ||
            '';

          if (startParam.startsWith('track_')) {
            const trackId = startParam.replace('track_', '').trim();
            setActiveTab('catalog');
            apiFetch(`/api/v1/app/tracks/${trackId}`)
              .then((res) => {
                if (res.ok) return res.json();
                throw new Error('Track not found');
              })
              .then((track) => {
                playTrack(track, [track]);
                setIsPlayerOpen(true);
              })
              .catch((err) => {
                console.warn('Deep-link track autoplay failed:', err);
              });
          } else if (
            startParam.startsWith('job_') ||
            startParam === 'queue' ||
            startParam === 'ingest'
          ) {
            setActiveTab('ingest');
          } else if (startParam === 'radar' || startParam.startsWith('release_')) {
            setActiveTab('radar');
          } else if (
            (startParam === 'governance' || startParam === 'admin') &&
            canManageUsers(data)
          ) {
            setActiveTab('governance');
          }
        }
      })
      .catch((err: Error) => {
        console.error('Failed to load user profile:', err);
        setError(err.message || 'Authentication error');
        setLoading(false);
      });
  }, [playTrack, setIsPlayerOpen]);

  const getRoleBadgeMode = (role?: string) => {
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

  const isManager = canManageUsers(profile);

const GuestCatalogLockedCard: React.FC<{ profile: UserProfile }> = ({ profile: _profile }) => {
  const [hasPending, setHasPending] = useState<boolean>(false);
  const [checking, setChecking] = useState<boolean>(true);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [submitted, setSubmitted] = useState<boolean>(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    apiFetch('/api/v1/app/access-request')
      .then((res) => res.json())
      .then((data) => {
        setHasPending(Boolean(data.has_pending));
        setChecking(false);
      })
      .catch(() => setChecking(false));
  }, []);

  const handleRequestAccess = async () => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      const res = await apiFetch('/api/v1/app/access-request', { method: 'POST' });
      const data = await res.json();
      if (res.ok && data.success) {
        setSubmitted(true);
        setHasPending(true);
      } else {
        setSubmitError(data.error || 'Failed to submit request');
      }
    } catch (err: unknown) {
      setSubmitError(err instanceof Error ? err.message : 'Network error');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Card
      style={{
        padding: '28px 20px',
        backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
        borderRadius: '16px',
        textAlign: 'center',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: '14px',
        border: '1px solid rgba(255, 159, 10, 0.25)',
      }}
    >
      <div
        style={{
          width: '56px',
          height: '56px',
          borderRadius: '50%',
          backgroundColor: 'rgba(255, 159, 10, 0.15)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <ShieldAlert size={28} color="#ff9f0a" />
      </div>
      <div>
        <Headline weight="2" style={{ fontSize: '18px', color: 'var(--tg-theme-text-color, #fff)' }}>
          Catalog Access Restricted
        </Headline>
        <Caption
          style={{
            color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
            marginTop: '6px',
            lineHeight: '1.4',
            display: 'block',
            maxWidth: '380px',
          }}
        >
          Your account holds the unprivileged <b>Guest</b> role. By default, access to library browsing and audio streaming is restricted until approved by an administrator.
        </Caption>
      </div>

      {checking ? (
        <Spinner size="s" />
      ) : hasPending || submitted ? (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            padding: '10px 16px',
            backgroundColor: 'rgba(48, 209, 88, 0.12)',
            borderRadius: '10px',
            color: '#30d158',
            fontSize: '13px',
            fontWeight: 600,
          }}
        >
          <Clock size={16} />
          <span>Access request pending review by administrators</span>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', width: '100%', maxWidth: '280px' }}>
          <button
            onClick={handleRequestAccess}
            disabled={submitting}
            style={{
              padding: '12px 20px',
              backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
              color: 'var(--tg-theme-button-text-color, #fff)',
              border: 'none',
              borderRadius: '10px',
              fontWeight: 600,
              fontSize: '14px',
              cursor: submitting ? 'wait' : 'pointer',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '6px',
              boxShadow: '0 2px 8px rgba(10, 132, 255, 0.3)',
            }}
          >
            {submitting ? <Spinner size="s" /> : <UserCheck size={16} />}
            <span>Request Catalog Access</span>
          </button>
          {submitError && (
            <Caption style={{ color: 'var(--tg-theme-destructive-text-color, #ff453a)', fontSize: '12px' }}>
              {submitError}
            </Caption>
          )}
        </div>
      )}
    </Card>
  );
};

  return (
    <AppRoot>
      <div
        style={{
          maxWidth: '640px',
          margin: '0 auto',
          padding: '16px 16px 80px 16px',
          display: 'flex',
          flexDirection: 'column',
          gap: '16px',
        }}
      >
        {/* Top Header Card */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', paddingBottom: '4px' }}>
          <div>
            <Headline weight="1" style={{ fontSize: '20px', display: 'flex', alignItems: 'center', gap: '8px' }}>
              <Music size={22} color="var(--tg-theme-button-color, #0a84ff)" />
              Music Studio &amp; Player
            </Headline>
            <Subheadline level="2" style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
              Self-Hosted Telegram Music System
            </Subheadline>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            {profile && (
              <button
                onClick={() => setIsSettingsOpen(true)}
                title="Audio &amp; Playback Settings"
                style={{
                  background: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                  border: 'none',
                  borderRadius: '50%',
                  width: '32px',
                  height: '32px',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  color: 'var(--tg-theme-text-color, #fff)',
                  cursor: 'pointer',
                }}
              >
                <SlidersHorizontal size={15} />
              </button>
            )}
            {profile && (
              <Badge mode={getRoleBadgeMode(profile.role)} type="number">
                {profile.role}
              </Badge>
            )}
          </div>
        </div>

        {/* Navigation Segmented Tabs */}
        {profile && (
          <div
            style={{
              display: 'flex',
              backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
              borderRadius: '10px',
              padding: '4px',
              gap: '4px',
              overflowX: 'auto',
              scrollbarWidth: 'none',
              msOverflowStyle: 'none',
              WebkitOverflowScrolling: 'touch',
            }}
          >
            <button
              onClick={() => setActiveTab('catalog')}
              style={{
                flex: '1 0 auto',
                minWidth: 'max-content',
                whiteSpace: 'nowrap',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: '6px',
                padding: '8px 12px',
                borderRadius: '8px',
                border: 'none',
                backgroundColor: activeTab === 'catalog' ? 'var(--tg-theme-bg-color, #1c1c1e)' : 'transparent',
                color: activeTab === 'catalog' ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                fontWeight: activeTab === 'catalog' ? 700 : 500,
                fontSize: '13px',
                cursor: 'pointer',
                boxShadow: activeTab === 'catalog' ? '0 1px 4px rgba(0,0,0,0.2)' : 'none',
              }}
            >
              <Library size={14} />
              <span>Catalog</span>
            </button>

            <button
              onClick={() => setActiveTab('radar')}
              style={{
                flex: '1 0 auto',
                minWidth: 'max-content',
                whiteSpace: 'nowrap',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: '6px',
                padding: '8px 12px',
                borderRadius: '8px',
                border: 'none',
                backgroundColor: activeTab === 'radar' ? 'var(--tg-theme-bg-color, #1c1c1e)' : 'transparent',
                color: activeTab === 'radar' ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                fontWeight: activeTab === 'radar' ? 700 : 500,
                fontSize: '13px',
                cursor: 'pointer',
                boxShadow: activeTab === 'radar' ? '0 1px 4px rgba(0,0,0,0.2)' : 'none',
              }}
            >
              <Radio size={14} />
              <span>Radar</span>
            </button>

            <button
              onClick={() => setActiveTab('ingest')}
              style={{
                flex: '1 0 auto',
                minWidth: 'max-content',
                whiteSpace: 'nowrap',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: '6px',
                padding: '8px 12px',
                borderRadius: '8px',
                border: 'none',
                backgroundColor: activeTab === 'ingest' ? 'var(--tg-theme-bg-color, #1c1c1e)' : 'transparent',
                color: activeTab === 'ingest' ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                fontWeight: activeTab === 'ingest' ? 700 : 500,
                fontSize: '13px',
                cursor: 'pointer',
                boxShadow: activeTab === 'ingest' ? '0 1px 4px rgba(0,0,0,0.2)' : 'none',
              }}
            >
              <UploadCloud size={14} />
              <span>Queue</span>
            </button>

            {/* Governance Tab: Feature locked and completely omitted for non-admins */}
            {isManager && (
              <button
                onClick={() => setActiveTab('governance')}
                style={{
                  flex: '1 0 auto',
                  minWidth: 'max-content',
                  whiteSpace: 'nowrap',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  gap: '6px',
                  padding: '8px 12px',
                  borderRadius: '8px',
                  border: 'none',
                  backgroundColor: activeTab === 'governance' ? 'var(--tg-theme-bg-color, #1c1c1e)' : 'transparent',
                  color: activeTab === 'governance' ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                  fontWeight: activeTab === 'governance' ? 700 : 500,
                  fontSize: '13px',
                  cursor: 'pointer',
                  boxShadow: activeTab === 'governance' ? '0 1px 4px rgba(0,0,0,0.2)' : 'none',
                }}
              >
                <Shield size={14} />
                <span>Admin</span>
              </button>
            )}

            <button
              onClick={() => setActiveTab('profile')}
              style={{
                flex: '1 0 auto',
                minWidth: 'max-content',
                whiteSpace: 'nowrap',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: '6px',
                padding: '8px 12px',
                borderRadius: '8px',
                border: 'none',
                backgroundColor: activeTab === 'profile' ? 'var(--tg-theme-bg-color, #1c1c1e)' : 'transparent',
                color: activeTab === 'profile' ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                fontWeight: activeTab === 'profile' ? 700 : 500,
                fontSize: '13px',
                cursor: 'pointer',
                boxShadow: activeTab === 'profile' ? '0 1px 4px rgba(0,0,0,0.2)' : 'none',
              }}
            >
              <UserCheck size={14} />
              <span>Account</span>
            </button>
          </div>
        )}

        {/* Loading Spinner */}
        {loading && (
          <div style={{ display: 'flex', justifyContent: 'center', padding: '60px 0' }}>
            <Spinner size="l" />
          </div>
        )}

        {/* Error / Unauthorized State */}
        {!loading && error && (
          <Card style={{ padding: '16px', border: '1px solid var(--tg-theme-destructive-text-color, #ff453a)' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              <AlertCircle size={24} color="var(--tg-theme-destructive-text-color, #ff453a)" />
              <div>
                <Headline weight="2">Authentication Required</Headline>
                <Caption style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                  {error}
                </Caption>
              </div>
            </div>
            <div style={{ marginTop: '12px' }}>
              <Caption>
                Please launch this Mini App directly from the Telegram Bot menu or an inline button to authenticate securely.
              </Caption>
            </div>
          </Card>
        )}

        {/* Tab Views */}
        {!loading && profile && activeTab === 'catalog' && (
          canViewCatalog(profile) ? (
            <CatalogView profile={profile} />
          ) : (
            <GuestCatalogLockedCard profile={profile} />
          )
        )}

        {!loading && profile && activeTab === 'radar' && (
          <RadarView profile={profile} />
        )}

        {!loading && profile && activeTab === 'ingest' && (
          <IngestQueueView profile={profile} />
        )}

        {!loading && profile && activeTab === 'governance' && isManager && (
          <GovernanceView profile={profile} />
        )}

        {!loading && profile && activeTab === 'profile' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            <Card style={{ padding: '16px', backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)', borderRadius: '12px' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <div>
                  <Headline weight="2">{profile.display_name}</Headline>
                  <Caption style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                    {profile.username ? `@${profile.username}` : `Telegram ID: ${profile.telegram_id}`}
                  </Caption>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <CheckCircle2 size={16} color="#30d158" />
                  <Caption style={{ color: '#30d158', fontWeight: 600 }}>Active Session</Caption>
                </div>
              </div>
              {profile.authentik_linked && (
                <div style={{ marginTop: '10px', paddingTop: '10px', borderTop: '1px solid var(--tg-theme-section-separator-color, #38383a)' }}>
                  <Caption style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                    🔐 Authentik SSO: <code>{profile.authentik_username}</code>
                  </Caption>
                </div>
              )}
            </Card>

            {/* User Settings & Preferences Card */}
            <Card
              onClick={() => setIsSettingsOpen(true)}
              style={{
                padding: '16px',
                backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                borderRadius: '12px',
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <SlidersHorizontal size={20} color="var(--tg-theme-button-color, #0a84ff)" />
                <div>
                  <Subheadline level="1" weight="2">Audio &amp; Streaming Preferences</Subheadline>
                  <Caption style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                    Transcoding format, streaming bitrates &amp; auto-lyrics
                  </Caption>
                </div>
              </div>
              <div style={{ color: 'var(--tg-theme-button-color, #0a84ff)', fontWeight: 600, fontSize: '13px' }}>
                Configure
              </div>
            </Card>

            {/* Granted Permissions Card */}
            <Card style={{ padding: '16px', backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)', borderRadius: '12px' }}>
              <Subheadline level="1" weight="2" style={{ marginBottom: '8px' }}>
                Role Permissions ({profile.permissions.length})
              </Subheadline>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                {profile.permissions.map((perm) => (
                  <span
                    key={perm}
                    style={{
                      fontSize: '11px',
                      backgroundColor: 'rgba(255, 255, 255, 0.08)',
                      color: 'var(--tg-theme-text-color, #fff)',
                      padding: '3px 8px',
                      borderRadius: '4px',
                    }}
                  >
                    {perm}
                  </span>
                ))}
              </div>
            </Card>
          </div>
        )}
      </div>

      {/* Settings Modal */}
      <SettingsModal
        isOpen={isSettingsOpen}
        onClose={() => setIsSettingsOpen(false)}
        profile={profile}
      />

      {/* Global Floating Player Components (only if authorized to view catalog) */}
      {profile && canViewCatalog(profile) && (
        <>
          <MiniPlayer />
          <FullscreenPlayer />
          <SyncedLyrics />
          <QueueDrawer />
        </>
      )}
    </AppRoot>
  );
};

export const App: React.FC = () => {
  return (
    <AudioPlayerProvider>
      <AppContent />
    </AudioPlayerProvider>
  );
};
