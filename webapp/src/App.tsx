import React, { useEffect, useState } from 'react';
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
  Sliders,
  UploadCloud,
  ShieldCheck,
  CheckCircle2,
  AlertCircle,
  Sparkles,
} from 'lucide-react';
import { fetchCurrentProfile, UserProfile } from './lib/auth';

export const App: React.FC = () => {
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchCurrentProfile()
      .then((data) => {
        setProfile(data);
        setLoading(false);
      })
      .catch((err: Error) => {
        console.error('Failed to load user profile:', err);
        setError(err.message || 'Authentication error');
        setLoading(false);
      });
  }, []);

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
      default:
        return 'gray';
    }
  };

  return (
    <AppRoot>
      <div style={{ maxWidth: '640px', margin: '0 auto', padding: '16px', display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {/* Top Header Card */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', paddingBottom: '8px' }}>
          <div>
            <Headline weight="1" style={{ fontSize: '20px', display: 'flex', alignItems: 'center', gap: '8px' }}>
              <Music size={22} color="var(--tg-theme-button-color, #0a84ff)" />
              Music Studio &amp; Player
            </Headline>
            <Subheadline level="2" style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
              Self-Hosted Telegram Music System
            </Subheadline>
          </div>
          {profile && (
            <Badge mode={getRoleBadgeMode(profile.role)} type="number">
              {profile.role}
            </Badge>
          )}
        </div>

        {/* Loading Spinner */}
        {loading && (
          <div style={{ display: 'flex', justifyContent: 'center', padding: '40px 0' }}>
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

        {/* Authenticated User Dashboard */}
        {!loading && profile && (
          <>
            {/* User Profile Card */}
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

            {/* Roadmap Features Grid */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              <Subheadline level="1" weight="2" style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', textTransform: 'uppercase', fontSize: '12px', letterSpacing: '0.5px' }}>
                Feature Modules
              </Subheadline>

              {/* Phase 2: Catalog & Player */}
              <Card style={{ padding: '16px', display: 'flex', flexDirection: 'column', gap: '8px', borderRadius: '12px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                  <div style={{ padding: '8px', borderRadius: '8px', backgroundColor: 'rgba(10, 132, 255, 0.15)' }}>
                    <Music size={20} color="var(--tg-theme-button-color, #0a84ff)" />
                  </div>
                  <div style={{ flex: 1 }}>
                    <Headline weight="2" style={{ fontSize: '16px' }}>Music Catalog &amp; Player</Headline>
                    <Caption style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                      Browse indexed library tracks, stream audio, and view synchronized lyrics.
                    </Caption>
                  </div>
                  <Badge type="number" mode="primary">Phase 2</Badge>
                </div>
              </Card>

              {/* Phase 3: Metadata Studio */}
              <Card style={{ padding: '16px', display: 'flex', flexDirection: 'column', gap: '8px', borderRadius: '12px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                  <div style={{ padding: '8px', borderRadius: '8px', backgroundColor: 'rgba(255, 159, 10, 0.15)' }}>
                    <Sliders size={20} color="#ff9f0a" />
                  </div>
                  <div style={{ flex: 1 }}>
                    <Headline weight="2" style={{ fontSize: '16px' }}>Metadata &amp; Lyrics Studio</Headline>
                    <Caption style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                      Edit tags, upload covers, and adjust LRC sync with dry-run verification.
                    </Caption>
                  </div>
                  <Badge type="number" mode="secondary">Phase 3</Badge>
                </div>
              </Card>

              {/* Phase 4: Ingestion Hub */}
              <Card style={{ padding: '16px', display: 'flex', flexDirection: 'column', gap: '8px', borderRadius: '12px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                  <div style={{ padding: '8px', borderRadius: '8px', backgroundColor: 'rgba(48, 209, 88, 0.15)' }}>
                    <UploadCloud size={20} color="#30d158" />
                  </div>
                  <div style={{ flex: 1 }}>
                    <Headline weight="2" style={{ fontSize: '16px' }}>Ingestion Hub &amp; Live Queue</Headline>
                    <Caption style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                      Upload lossless audio or archives and monitor live 10-stage processing.
                    </Caption>
                  </div>
                  <Badge type="number" mode="secondary">Phase 4</Badge>
                </div>
              </Card>

              {/* Phase 5: Governance & Admin */}
              <Card style={{ padding: '16px', display: 'flex', flexDirection: 'column', gap: '8px', borderRadius: '12px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                  <div style={{ padding: '8px', borderRadius: '8px', backgroundColor: 'rgba(191, 90, 242, 0.15)' }}>
                    <ShieldCheck size={20} color="#bf5af2" />
                  </div>
                  <div style={{ flex: 1 }}>
                    <Headline weight="2" style={{ fontSize: '16px' }}>Governance &amp; Controls</Headline>
                    <Caption style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                      Manage user tiers, access requests, and trigger library folder rescan.
                    </Caption>
                  </div>
                  <Badge type="number" mode="secondary">Phase 5</Badge>
                </div>
              </Card>
            </div>

            {/* Permissions & Security Overview */}
            <Card style={{ padding: '16px', backgroundColor: 'var(--tg-theme-section-bg-color, #2c2c2e)', borderRadius: '12px' }}>
              <Headline weight="3" style={{ fontSize: '14px', marginBottom: '8px', display: 'flex', alignItems: 'center', gap: '6px' }}>
                <Sparkles size={16} color="var(--tg-theme-button-color, #0a84ff)" />
                Assigned RBAC Permissions
              </Headline>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                {profile.permissions.map((perm) => (
                  <span
                    key={perm}
                    style={{
                      fontSize: '11px',
                      padding: '4px 8px',
                      borderRadius: '6px',
                      backgroundColor: 'rgba(255, 255, 255, 0.08)',
                      color: 'var(--tg-theme-text-color, #f2f2f7)',
                      fontFamily: 'monospace',
                    }}
                  >
                    {perm}
                  </span>
                ))}
              </div>
            </Card>
          </>
        )}
      </div>
    </AppRoot>
  );
};

