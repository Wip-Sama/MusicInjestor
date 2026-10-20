import React, { useEffect, useState, useCallback } from 'react';
import {
  Radio,
  Play,
  Pause,
  ExternalLink,
  Bell,
  BellOff,
  RefreshCw,
  Search,
  Disc,
  Users,
  Music,
} from 'lucide-react';
import { Card, Subheadline, Caption, Spinner } from '@telegram-apps/telegram-ui';
import { useAudioPlayer } from '../context/AudioPlayerContext';
import { apiFetch } from '../lib/auth';
import { RadarRelease, RadarFollowItem, TrackItem } from '../types/music';

interface RadarViewProps {
  profile?: unknown;
}

const triggerHaptic = (style: 'light' | 'medium' | 'heavy' = 'medium') => {
  try {
    const tg = (
      window as unknown as {
        Telegram?: { WebApp?: { HapticFeedback?: { impactOccurred: (s: string) => void } } };
      }
    ).Telegram;
    tg?.WebApp?.HapticFeedback?.impactOccurred(style);
  } catch {
    // ignore outside Telegram
  }
};

const triggerNotificationHaptic = (type: 'error' | 'success' | 'warning' = 'success') => {
  try {
    const tg = (
      window as unknown as {
        Telegram?: { WebApp?: { HapticFeedback?: { notificationOccurred: (t: string) => void } } };
      }
    ).Telegram;
    tg?.WebApp?.HapticFeedback?.notificationOccurred(type);
  } catch {
    // ignore outside Telegram
  }
};

export const RadarView: React.FC<RadarViewProps> = () => {
  const { currentTrack, isPlaying, playTrack, togglePlay, toggleFollowArtist } = useAudioPlayer();
  const [releases, setReleases] = useState<RadarRelease[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [filterFollowed, setFilterFollowed] = useState<boolean>(false);
  const [page] = useState<number>(1);
  const [newArtistName, setNewArtistName] = useState<string>('');
  const [followingArtist, setFollowingArtist] = useState<boolean>(false);
  const [followedCount, setFollowedCount] = useState<number>(0);
  const [activeTab, setActiveTab] = useState<'releases' | 'follows'>('releases');
  const [followsList, setFollowsList] = useState<RadarFollowItem[]>([]);
  const [loadingFollows, setLoadingFollows] = useState<boolean>(false);

  const loadReleases = useCallback(() => {
    setLoading(true);
    const params = new URLSearchParams({
      page: page.toString(),
      per_page: '30',
      followed_only: filterFollowed ? 'true' : 'false',
    });

    apiFetch(`/api/v1/app/radar/releases?${params.toString()}`)
      .then((res) => res.json())
      .then((data) => {
        setReleases(data.items || []);
        setFollowedCount(data.followed_count || 0);
        setLoading(false);
      })
      .catch((err) => {
        console.error('Failed to load radar releases:', err);
        setLoading(false);
      });
  }, [filterFollowed, page]);

  const loadFollows = useCallback(() => {
    setLoadingFollows(true);
    apiFetch('/api/v1/app/radar/follows')
      .then((res) => res.json())
      .then((data) => {
        setFollowsList(data.items || []);
        setFollowedCount((data.items || []).length);
        setLoadingFollows(false);
      })
      .catch((err) => {
        console.error('Failed to load followed artists:', err);
        setLoadingFollows(false);
      });
  }, []);

  useEffect(() => {
    if (activeTab === 'releases') {
      loadReleases();
    } else {
      loadFollows();
    }
  }, [activeTab, loadReleases, loadFollows]);

  const handleToggleFollow = async (artistName: string, _currentlyFollowed?: boolean) => {
    try {
      const nextFollow = await toggleFollowArtist(artistName);
      setReleases((prev) =>
        prev.map((r) =>
          r.artist_name.toLowerCase() === artistName.toLowerCase()
            ? { ...r, is_followed: nextFollow }
            : r
        )
      );
      if (activeTab === 'follows') {
        loadFollows();
      }
    } catch (err) {
      console.error('Failed to toggle artist follow:', err);
    }
  };

  const handleAddNewArtistFollow = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newArtistName.trim()) return;

    setFollowingArtist(true);
    try {
      const res = await apiFetch('/api/v1/app/radar/follow', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ artist_name: newArtistName.trim(), follow: true }),
      });
      if (res.ok) {
        setNewArtistName('');
        triggerNotificationHaptic('success');
        loadReleases();
        if (activeTab === 'follows') {
          loadFollows();
        }
      }
    } catch (err) {
      console.error('Failed to follow artist:', err);
    } finally {
      setFollowingArtist(false);
    }
  };

  const handleTriggerScan = async () => {
    setRefreshing(true);
    try {
      triggerHaptic('heavy');
      await apiFetch('/api/v1/app/radar/scan', { method: 'POST' });
      loadReleases();
    } catch (err) {
      console.error('Failed to trigger scan:', err);
    } finally {
      setRefreshing(false);
    }
  };

  const handlePlayPreview = (release: RadarRelease) => {
    if (!release.preview_url) return;

    if (currentTrack?.id === release.id) {
      togglePlay();
      return;
    }

    const previewTrack: TrackItem = {
      id: release.id,
      title: `${release.title} (Preview)`,
      artist: release.artist_name,
      album: release.title,
      year: release.release_date ? parseInt(release.release_date.slice(0, 4)) : null,
      track_number: 1,
      duration_seconds: 30,
      has_synced_lyrics: false,
      format: 'mp3',
      audio_url: release.preview_url,
      cover_url: release.cover_url || '',
    };
    playTrack(previewTrack, [previewTrack]);
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '14px', paddingBottom: '90px' }}>
      {/* Top Banner Card */}
      <Card style={{ padding: '16px', backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)', borderRadius: '12px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <div style={{ backgroundColor: '#0a84ff22', padding: '8px', borderRadius: '10px' }}>
              <Radio size={22} color="var(--tg-theme-button-color, #0a84ff)" />
            </div>
            <div>
              <Subheadline level="1" weight="2">Release Radar</Subheadline>
              <Caption style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                New drops from catalog artists &amp; followed authors
              </Caption>
            </div>
          </div>
          <button
            onClick={handleTriggerScan}
            disabled={refreshing}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              padding: '6px 10px',
              borderRadius: '8px',
              border: 'none',
              backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
              color: 'var(--tg-theme-button-text-color, #fff)',
              cursor: refreshing ? 'default' : 'pointer',
              fontSize: '12px',
              fontWeight: 600,
            }}
          >
            <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} />
            <span>{refreshing ? 'Scanning...' : 'Scan'}</span>
          </button>
        </div>

        {/* Follow new artist input form */}
        <form onSubmit={handleAddNewArtistFollow} style={{ display: 'flex', gap: '8px', marginTop: '14px' }}>
          <div
            style={{
              flex: 1,
              display: 'flex',
              alignItems: 'center',
              backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
              borderRadius: '8px',
              padding: '0 10px',
            }}
          >
            <Search size={15} color="var(--tg-theme-subtitle-text-color, #8e8e93)" />
            <input
              type="text"
              placeholder="Follow an artist (e.g. Taylor Swift, YOASOBI)..."
              value={newArtistName}
              onChange={(e) => setNewArtistName(e.target.value)}
              style={{
                width: '100%',
                padding: '9px 8px',
                border: 'none',
                background: 'transparent',
                color: 'var(--tg-theme-text-color, #fff)',
                fontSize: '13px',
                outline: 'none',
              }}
            />
          </div>
          <button
            type="submit"
            disabled={followingArtist || !newArtistName.trim()}
            style={{
              padding: '0 14px',
              borderRadius: '8px',
              border: 'none',
              backgroundColor: newArtistName.trim() ? 'var(--tg-theme-button-color, #0a84ff)' : 'rgba(255,255,255,0.08)',
              color: newArtistName.trim() ? '#fff' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
              cursor: newArtistName.trim() ? 'pointer' : 'default',
              fontSize: '13px',
              fontWeight: 600,
            }}
          >
            Follow
          </button>
        </form>
      </Card>

      {/* Sub-view selector tabs: Releases vs Followed Artists */}
      <div style={{ display: 'flex', gap: '8px' }}>
        <button
          onClick={() => { setActiveTab('releases'); setFilterFollowed(false); }}
          style={{
            flex: 1,
            padding: '8px',
            borderRadius: '8px',
            border: 'none',
            backgroundColor: activeTab === 'releases' && !filterFollowed ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
            color: activeTab === 'releases' && !filterFollowed ? '#fff' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
            fontWeight: 600,
            fontSize: '13px',
            cursor: 'pointer',
          }}
        >
          All Drops
        </button>

        <button
          onClick={() => { setActiveTab('releases'); setFilterFollowed(true); }}
          style={{
            flex: 1,
            padding: '8px',
            borderRadius: '8px',
            border: 'none',
            backgroundColor: activeTab === 'releases' && filterFollowed ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
            color: activeTab === 'releases' && filterFollowed ? '#fff' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
            fontWeight: 600,
            fontSize: '13px',
            cursor: 'pointer',
          }}
        >
          Followed Only
        </button>

        <button
          onClick={() => setActiveTab('follows')}
          style={{
            flex: 1,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '5px',
            padding: '8px',
            borderRadius: '8px',
            border: 'none',
            backgroundColor: activeTab === 'follows' ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
            color: activeTab === 'follows' ? '#fff' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
            fontWeight: 600,
            fontSize: '13px',
            cursor: 'pointer',
          }}
        >
          <Users size={14} />
          <span>Follows ({followedCount})</span>
        </button>
      </div>

      {/* Main Content Area */}
      {activeTab === 'releases' ? (
        loading ? (
          <div style={{ display: 'flex', justifyContent: 'center', padding: '40px 0' }}>
            <Spinner size="m" />
          </div>
        ) : releases.length === 0 ? (
          <Card style={{ padding: '24px', textAlign: 'center', backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)', borderRadius: '12px' }}>
            <Disc size={36} color="var(--tg-theme-subtitle-text-color, #8e8e93)" style={{ margin: '0 auto 10px' }} />
            <Subheadline level="1" weight="2">No Releases Discovered</Subheadline>
            <Caption style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', marginTop: '4px' }}>
              {filterFollowed
                ? "You haven't received drops from followed artists yet, or you don't follow any artists."
                : "Tap 'Scan' above to fetch recent releases from Deezer for artists in your catalog!"}
            </Caption>
          </Card>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            {releases.map((rel) => {
              const isCurrentlyPlaying = currentTrack?.id === rel.id && isPlaying;
              return (
                <Card
                  key={rel.id}
                  style={{
                    padding: '12px',
                    backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                    borderRadius: '10px',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '10px',
                  }}
                >
                  <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
                    {/* Album Cover */}
                    <div
                      style={{
                        width: '64px',
                        height: '64px',
                        borderRadius: '8px',
                        backgroundColor: '#1c1c1e',
                        overflow: 'hidden',
                        flexShrink: 0,
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        position: 'relative',
                      }}
                    >
                      {rel.cover_url ? (
                        <img
                          src={rel.cover_url}
                          alt={rel.title}
                          style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                        />
                      ) : (
                        <Music size={28} color="#8e8e93" />
                      )}

                      {/* Play Preview Overlay Button */}
                      {rel.preview_url && (
                        <button
                          onClick={() => handlePlayPreview(rel)}
                          style={{
                            position: 'absolute',
                            inset: 0,
                            backgroundColor: isCurrentlyPlaying ? 'rgba(10, 132, 255, 0.7)' : 'rgba(0, 0, 0, 0.35)',
                            border: 'none',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            cursor: 'pointer',
                            color: '#fff',
                          }}
                        >
                          {isCurrentlyPlaying ? <Pause size={22} /> : <Play size={22} style={{ marginLeft: '2px' }} />}
                        </button>
                      )}
                    </div>

                    {/* Metadata */}
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                        <span
                          style={{
                            fontSize: '10px',
                            fontWeight: 700,
                            padding: '1px 5px',
                            borderRadius: '4px',
                            backgroundColor: rel.release_type === 'album' ? '#30d15822' : '#0a84ff22',
                            color: rel.release_type === 'album' ? '#30d158' : '#0a84ff',
                            textTransform: 'uppercase',
                          }}
                        >
                          {rel.release_type}
                        </span>
                        {rel.in_library && (
                          <span
                            style={{
                              fontSize: '10px',
                              fontWeight: 700,
                              padding: '1px 5px',
                              borderRadius: '4px',
                              backgroundColor: '#ff9f0a22',
                              color: '#ff9f0a',
                            }}
                          >
                            In Library
                          </span>
                        )}
                        <span style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                          {rel.release_date}
                        </span>
                      </div>

                      <div
                        style={{
                          fontWeight: 600,
                          fontSize: '14px',
                          color: 'var(--tg-theme-text-color, #fff)',
                          whiteSpace: 'nowrap',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          marginTop: '3px',
                        }}
                      >
                        {rel.title}
                      </div>

                      <div
                        style={{
                          fontSize: '13px',
                          color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                          whiteSpace: 'nowrap',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                        }}
                      >
                        {rel.artist_name}
                      </div>
                    </div>
                  </div>

                  {/* Action Buttons Row */}
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      borderTop: '1px solid rgba(255, 255, 255, 0.06)',
                      paddingTop: '8px',
                    }}
                  >
                    {/* Follow / Unfollow Toggle */}
                    <button
                      onClick={() => handleToggleFollow(rel.artist_name, rel.is_followed)}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: '5px',
                        padding: '5px 8px',
                        borderRadius: '6px',
                        border: 'none',
                        backgroundColor: rel.is_followed ? '#30d15822' : 'rgba(255, 255, 255, 0.08)',
                        color: rel.is_followed ? '#30d158' : 'var(--tg-theme-text-color, #fff)',
                        cursor: 'pointer',
                        fontSize: '12px',
                        fontWeight: 600,
                      }}
                    >
                      {rel.is_followed ? <Bell size={13} /> : <BellOff size={13} />}
                      <span>{rel.is_followed ? 'Following' : 'Follow Artist'}</span>
                    </button>

                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      {rel.preview_url && (
                        <button
                          onClick={() => handlePlayPreview(rel)}
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: '4px',
                            padding: '5px 8px',
                            borderRadius: '6px',
                            border: 'none',
                            backgroundColor: 'rgba(255, 255, 255, 0.08)',
                            color: 'var(--tg-theme-button-color, #0a84ff)',
                            cursor: 'pointer',
                            fontSize: '12px',
                            fontWeight: 600,
                          }}
                        >
                          {isCurrentlyPlaying ? <Pause size={13} /> : <Play size={13} />}
                          <span>{isCurrentlyPlaying ? 'Pause' : '30s Preview'}</span>
                        </button>
                      )}

                      {rel.external_url && (
                        <a
                          href={rel.external_url}
                          target="_blank"
                          rel="noreferrer"
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: '4px',
                            padding: '5px 8px',
                            borderRadius: '6px',
                            backgroundColor: 'rgba(255, 255, 255, 0.08)',
                            color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                            textDecoration: 'none',
                            fontSize: '12px',
                          }}
                        >
                          <ExternalLink size={13} />
                          <span>Deezer</span>
                        </a>
                      )}
                    </div>
                  </div>
                </Card>
              );
            })}
          </div>
        )
      ) : (
        /* Followed Artists Management View */
        loadingFollows ? (
          <div style={{ display: 'flex', justifyContent: 'center', padding: '40px 0' }}>
            <Spinner size="m" />
          </div>
        ) : followsList.length === 0 ? (
          <Card style={{ padding: '24px', textAlign: 'center', backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)', borderRadius: '12px' }}>
            <Users size={36} color="var(--tg-theme-subtitle-text-color, #8e8e93)" style={{ margin: '0 auto 10px' }} />
            <Subheadline level="1" weight="2">No Followed Artists</Subheadline>
            <Caption style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', marginTop: '4px' }}>
              Type an artist name in the box above to start receiving alerts when they publish new music!
            </Caption>
          </Card>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {followsList.map((item) => (
              <Card
                key={item.id}
                style={{
                  padding: '12px 16px',
                  backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                  borderRadius: '10px',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                }}
              >
                <div>
                  <div style={{ fontWeight: 600, fontSize: '14px', color: 'var(--tg-theme-text-color, #fff)' }}>
                    {item.artist_name}
                  </div>
                  <Caption style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                    Following since {item.created_at.slice(0, 10)}
                  </Caption>
                </div>
                <button
                  onClick={() => handleToggleFollow(item.artist_name, true)}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '5px',
                    padding: '6px 10px',
                    borderRadius: '6px',
                    border: 'none',
                    backgroundColor: 'rgba(255, 69, 58, 0.15)',
                    color: '#ff453a',
                    cursor: 'pointer',
                    fontSize: '12px',
                    fontWeight: 600,
                  }}
                >
                  <BellOff size={13} />
                  <span>Unfollow</span>
                </button>
              </Card>
            ))}
          </div>
        )
      )}
    </div>
  );
};

