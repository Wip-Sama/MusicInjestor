import React, { useEffect, useState, useCallback, useRef } from 'react';
import {
  X,
  Play,
  Pause,
  Bell,
  BellOff,
  Music,
  Disc,
  ExternalLink,
  Check,
  Radio,
  Send,
  Sparkles,
} from 'lucide-react';
import { Card, Subheadline, Caption, Spinner } from '@telegram-apps/telegram-ui';
import { useAudioPlayer } from '../../context/AudioPlayerContext';
import { apiFetch } from '../../lib/auth';
import { ArtistDetailsResponse, RadarRelease, TrackItem } from '../../types/music';

interface ArtistModalProps {
  artistName: string | null;
  isOpen: boolean;
  onClose: () => void;
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
    // Ignore outside Telegram
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
    // Ignore outside Telegram
  }
};

export const ArtistModal: React.FC<ArtistModalProps> = ({ artistName, isOpen, onClose }) => {
  const { currentTrack, isPlaying, playTrack, togglePlay, isArtistFollowed, toggleFollowArtist, sendTrackToChat } =
    useAudioPlayer();
  const [data, setData] = useState<ArtistDetailsResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [activeTab, setActiveTab] = useState<'library' | 'radar'>('library');
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [sendingTrackId, setSendingTrackId] = useState<string | null>(null);
  const [togglingFollow, setTogglingFollow] = useState<boolean>(false);
  const longPressTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isLongPressTriggeredRef = useRef<boolean>(false);

  const loadArtistDetails = useCallback(() => {
    if (!artistName) return;
    setLoading(true);
    apiFetch(`/api/v1/app/artists/${encodeURIComponent(artistName)}`)
      .then((res) => {
        if (!res.ok) throw new Error('Artist not found');
        return res.json();
      })
      .then((resData: ArtistDetailsResponse) => {
        setData(resData);
        setLoading(false);
      })
      .catch((err) => {
        console.error('Failed to load artist details:', err);
        setLoading(false);
      });
  }, [artistName]);

  useEffect(() => {
    if (isOpen && artistName) {
      loadArtistDetails();
    } else {
      setData(null);
    }
  }, [isOpen, artistName, loadArtistDetails]);

  if (!isOpen || !artistName) return null;

  const isFollowed = isArtistFollowed(artistName);

  const handleToggleFollow = async () => {
    setTogglingFollow(true);
    try {
      triggerHaptic('medium');
      await toggleFollowArtist(artistName);
      setData((prev) => (prev ? { ...prev, is_followed: !isFollowed } : prev));
      triggerNotificationHaptic('success');
    } catch (err) {
      console.error('Failed to toggle artist follow:', err);
    } finally {
      setTogglingFollow(false);
    }
  };

  const handleSendToChat = async (trackId: string) => {
    setSendingTrackId(trackId);
    try {
      const res = await sendTrackToChat(trackId);
      if (res.ok) {
        triggerNotificationHaptic('success');
      } else {
        triggerNotificationHaptic('error');
      }
    } catch {
      triggerNotificationHaptic('error');
    } finally {
      setSendingTrackId(null);
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

  const handlePlayFullTrack = async (trackId: string) => {
    if (currentTrack?.id === trackId) {
      togglePlay();
      return;
    }
    try {
      const res = await apiFetch(`/api/v1/app/tracks/${trackId}`);
      if (res.ok) {
        const fullTrack: TrackItem = await res.json();
        playTrack(fullTrack, data?.library_tracks || [fullTrack]);
      }
    } catch (err) {
      console.error('Error fetching track:', err);
    }
  };

  const startLongPress = (id: string, url: string) => {
    isLongPressTriggeredRef.current = false;
    if (longPressTimerRef.current) clearTimeout(longPressTimerRef.current);
    longPressTimerRef.current = setTimeout(() => {
      isLongPressTriggeredRef.current = true;
      navigator.clipboard.writeText(url).catch(() => {});
      triggerNotificationHaptic('success');
      setCopiedId(id);
      setTimeout(() => setCopiedId(null), 2000);
    }, 450);
  };

  const cancelLongPress = () => {
    if (longPressTimerRef.current) {
      clearTimeout(longPressTimerRef.current);
      longPressTimerRef.current = null;
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="artist-modal-title"
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 1000,
        backgroundColor: 'rgba(0, 0, 0, 0.75)',
        backdropFilter: 'blur(10px)',
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'flex-end',
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        style={{
          backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
          borderTopLeftRadius: '20px',
          borderTopRightRadius: '20px',
          maxHeight: '90vh',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
          boxShadow: '0 -4px 24px rgba(0, 0, 0, 0.5)',
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: '16px 20px',
            borderBottom: '1px solid rgba(255, 255, 255, 0.08)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            position: 'relative',
          }}
        >
          <div style={{ minWidth: 0, flex: 1, paddingRight: '12px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <Sparkles size={16} color="var(--tg-theme-button-color, #0a84ff)" />
              <span style={{ fontSize: '11px', textTransform: 'uppercase', letterSpacing: '0.05em', color: '#8e8e93', fontWeight: 700 }}>
                Artist Dossier
              </span>
            </div>
            <h2
              id="artist-modal-title"
              style={{
                fontSize: '20px',
                fontWeight: 700,
                color: 'var(--tg-theme-text-color, #fff)',
                margin: '4px 0 0 0',
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
            >
              {artistName}
            </h2>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <button
              onClick={handleToggleFollow}
              disabled={togglingFollow}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '5px',
                padding: '6px 12px',
                borderRadius: '8px',
                border: 'none',
                backgroundColor: isFollowed ? 'rgba(255, 69, 58, 0.15)' : 'rgba(10, 132, 255, 0.15)',
                color: isFollowed ? '#ff453a' : 'var(--tg-theme-button-color, #0a84ff)',
                fontWeight: 600,
                fontSize: '12px',
                cursor: 'pointer',
              }}
            >
              {isFollowed ? <BellOff size={14} /> : <Bell size={14} />}
              <span>{isFollowed ? 'Unfollow' : 'Follow'}</span>
            </button>

            <button
              onClick={onClose}
              aria-label="Close"
              style={{
                width: '32px',
                height: '32px',
                borderRadius: '50%',
                backgroundColor: 'rgba(255, 255, 255, 0.08)',
                border: 'none',
                color: '#fff',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                cursor: 'pointer',
              }}
            >
              <X size={18} />
            </button>
          </div>
        </div>

        {/* Sub-tab Navigation */}
        <div style={{ display: 'flex', padding: '10px 16px', gap: '8px', borderBottom: '1px solid rgba(255, 255, 255, 0.06)' }}>
          <button
            onClick={() => setActiveTab('library')}
            style={{
              flex: 1,
              padding: '8px 12px',
              borderRadius: '8px',
              border: 'none',
              backgroundColor: activeTab === 'library' ? 'var(--tg-theme-button-color, #0a84ff)' : 'rgba(255, 255, 255, 0.06)',
              color: activeTab === 'library' ? '#fff' : '#8e8e93',
              fontSize: '13px',
              fontWeight: 600,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '6px',
            }}
          >
            <Music size={14} />
            <span>Library ({data?.library_tracks?.length ?? 0})</span>
          </button>

          <button
            onClick={() => setActiveTab('radar')}
            style={{
              flex: 1,
              padding: '8px 12px',
              borderRadius: '8px',
              border: 'none',
              backgroundColor: activeTab === 'radar' ? 'var(--tg-theme-button-color, #0a84ff)' : 'rgba(255, 255, 255, 0.06)',
              color: activeTab === 'radar' ? '#fff' : '#8e8e93',
              fontSize: '13px',
              fontWeight: 600,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '6px',
            }}
          >
            <Radio size={14} />
            <span>Radar Drops ({data?.discovered_releases?.length ?? 0})</span>
          </button>
        </div>

        {/* Content body */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '16px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
          {loading ? (
            <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', padding: '60px 0' }}>
              <Spinner size="m" />
            </div>
          ) : activeTab === 'library' ? (
            !data?.library_tracks || data.library_tracks.length === 0 ? (
              <Card style={{ padding: '24px', textAlign: 'center', backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)', borderRadius: '12px' }}>
                <Music size={36} color="#8e8e93" style={{ margin: '0 auto 10px' }} />
                <Subheadline level="1" weight="2">No Songs In Library</Subheadline>
                <Caption style={{ color: '#8e8e93', marginTop: '4px' }}>
                  We don&apos;t have any songs by {artistName} stored locally in the library yet.
                </Caption>
              </Card>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {data.library_tracks.map((trk) => {
                  const isTrkPlaying = currentTrack?.id === trk.id && isPlaying;
                  const isSending = sendingTrackId === trk.id;
                  return (
                    <Card
                      key={trk.id}
                      style={{
                        padding: '10px 12px',
                        backgroundColor: isTrkPlaying ? 'rgba(10, 132, 255, 0.12)' : 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                        borderRadius: '10px',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        gap: '10px',
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0, flex: 1 }}>
                        <button
                          onClick={() => {
                            if (currentTrack?.id === trk.id) {
                              togglePlay();
                            } else {
                              playTrack(trk, data.library_tracks);
                            }
                          }}
                          style={{
                            width: '36px',
                            height: '36px',
                            borderRadius: '50%',
                            border: 'none',
                            backgroundColor: isTrkPlaying ? '#30d158' : 'var(--tg-theme-button-color, #0a84ff)',
                            color: '#fff',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            cursor: 'pointer',
                            flexShrink: 0,
                          }}
                        >
                          {isTrkPlaying ? <Pause size={16} /> : <Play size={16} style={{ marginLeft: '2px' }} />}
                        </button>

                        <div style={{ minWidth: 0, flex: 1 }}>
                          <div
                            style={{
                              fontSize: '14px',
                              fontWeight: 600,
                              color: isTrkPlaying ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-text-color, #fff)',
                              whiteSpace: 'nowrap',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                            }}
                          >
                            {trk.title}
                          </div>
                          <div style={{ fontSize: '12px', color: '#8e8e93', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                            {trk.album || 'Unknown Album'} {trk.year ? `(${trk.year})` : ''}
                          </div>
                        </div>
                      </div>

                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexShrink: 0 }}>
                        <span
                          style={{
                            fontSize: '10px',
                            fontWeight: 700,
                            padding: '2px 5px',
                            borderRadius: '4px',
                            backgroundColor: 'rgba(255, 255, 255, 0.08)',
                            color: '#8e8e93',
                            textTransform: 'uppercase',
                          }}
                        >
                          {trk.format}
                        </span>

                        <button
                          onClick={() => handleSendToChat(trk.id)}
                          disabled={isSending}
                          title="Send audio file to Telegram chat"
                          style={{
                            background: 'none',
                            border: 'none',
                            padding: '6px',
                            color: isSending ? 'var(--tg-theme-button-color, #0a84ff)' : '#8e8e93',
                            cursor: isSending ? 'default' : 'pointer',
                            display: 'flex',
                            alignItems: 'center',
                          }}
                        >
                          <Send size={15} className={isSending ? 'animate-pulse' : ''} />
                        </button>
                      </div>
                    </Card>
                  );
                })}
              </div>
            )
          ) : (
            !data?.discovered_releases || data.discovered_releases.length === 0 ? (
              <Card style={{ padding: '24px', textAlign: 'center', backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)', borderRadius: '12px' }}>
                <Disc size={36} color="#8e8e93" style={{ margin: '0 auto 10px' }} />
                <Subheadline level="1" weight="2">No Releases On Radar</Subheadline>
                <Caption style={{ color: '#8e8e93', marginTop: '4px' }}>
                  No published drops or upcoming albums are currently cached for {artistName}. Follow them to receive alerts when they release music!
                </Caption>
              </Card>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                {data.discovered_releases.map((rel) => {
                  const isCurrentRelPlaying = currentTrack?.id === rel.id && isPlaying;
                  return (
                    <Card
                      key={rel.id}
                      style={{
                        padding: '12px',
                        backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                        borderRadius: '10px',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: '8px',
                      }}
                    >
                      <div style={{ display: 'flex', gap: '10px', alignItems: 'center' }}>
                        <div
                          style={{
                            width: '56px',
                            height: '56px',
                            borderRadius: '8px',
                            backgroundColor: '#1c1c1e',
                            overflow: 'hidden',
                            flexShrink: 0,
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                          }}
                        >
                          {rel.cover_url ? (
                            <img src={rel.cover_url} alt={rel.title} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                          ) : (
                            <Disc size={26} color="#8e8e93" />
                          )}
                        </div>

                        <div style={{ minWidth: 0, flex: 1 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                            <span
                              style={{
                                fontSize: '10px',
                                fontWeight: 700,
                                padding: '1px 5px',
                                borderRadius: '4px',
                                textTransform: 'uppercase',
                                backgroundColor: rel.in_library ? 'rgba(48, 209, 88, 0.2)' : 'rgba(10, 132, 255, 0.2)',
                                color: rel.in_library ? '#30d158' : '#0a84ff',
                              }}
                            >
                              {rel.in_library ? 'In Library' : 'New Drop'}
                            </span>
                            <span style={{ fontSize: '11px', color: '#8e8e93' }}>
                              {rel.release_date || 'Unknown date'}
                            </span>
                          </div>

                          <div
                            style={{
                              fontSize: '14px',
                              fontWeight: 700,
                              color: 'var(--tg-theme-text-color, #fff)',
                              margin: '2px 0',
                              whiteSpace: 'nowrap',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                            }}
                          >
                            {rel.title}
                          </div>

                          <div style={{ fontSize: '11px', color: '#8e8e93' }}>
                            {rel.release_type.toUpperCase()} • {rel.track_count} {rel.track_count === 1 ? 'track' : 'tracks'}
                          </div>
                        </div>
                      </div>

                      {/* Action buttons */}
                      <div style={{ display: 'flex', gap: '8px', marginTop: '4px' }}>
                        {rel.in_library && rel.library_track_id && rel.track_count === 1 ? (
                          <button
                            onClick={() => handlePlayFullTrack(rel.library_track_id!)}
                            style={{
                              flex: 1,
                              display: 'flex',
                              alignItems: 'center',
                              justifyContent: 'center',
                              gap: '5px',
                              padding: '7px 10px',
                              borderRadius: '6px',
                              border: 'none',
                              backgroundColor: currentTrack?.id === rel.library_track_id && isPlaying ? '#30d158' : 'rgba(48, 209, 88, 0.15)',
                              color: currentTrack?.id === rel.library_track_id && isPlaying ? '#fff' : '#30d158',
                              fontWeight: 600,
                              fontSize: '12px',
                              cursor: 'pointer',
                            }}
                          >
                            {currentTrack?.id === rel.library_track_id && isPlaying ? <Pause size={13} /> : <Play size={13} />}
                            <span>Play Full Track</span>
                          </button>
                        ) : rel.preview_url ? (
                          <button
                            onClick={() => handlePlayPreview(rel)}
                            style={{
                              flex: 1,
                              display: 'flex',
                              alignItems: 'center',
                              justifyContent: 'center',
                              gap: '5px',
                              padding: '7px 10px',
                              borderRadius: '6px',
                              border: 'none',
                              backgroundColor: isCurrentRelPlaying ? 'var(--tg-theme-button-color, #0a84ff)' : 'rgba(255, 255, 255, 0.08)',
                              color: isCurrentRelPlaying ? '#fff' : 'var(--tg-theme-text-color, #fff)',
                              fontWeight: 600,
                              fontSize: '12px',
                              cursor: 'pointer',
                            }}
                          >
                            {isCurrentRelPlaying ? <Pause size={13} /> : <Play size={13} />}
                            <span>30s Preview</span>
                          </button>
                        ) : null}

                        {rel.primary_url && (
                          <a
                            href={rel.primary_url}
                            target="_blank"
                            rel="noreferrer"
                            onMouseDown={() => startLongPress(rel.id, rel.primary_url!)}
                            onMouseUp={cancelLongPress}
                            onTouchStart={() => startLongPress(rel.id, rel.primary_url!)}
                            onTouchEnd={cancelLongPress}
                            style={{
                              display: 'flex',
                              alignItems: 'center',
                              gap: '4px',
                              padding: '7px 10px',
                              borderRadius: '6px',
                              backgroundColor: 'rgba(255, 255, 255, 0.08)',
                              color: copiedId === rel.id ? '#30d158' : 'var(--tg-theme-button-color, #0a84ff)',
                              textDecoration: 'none',
                              fontSize: '12px',
                              fontWeight: 600,
                              cursor: 'pointer',
                            }}
                          >
                            {copiedId === rel.id ? <Check size={13} /> : <ExternalLink size={13} />}
                            <span>{copiedId === rel.id ? 'Copied' : (rel.primary_platform || 'Link')}</span>
                          </a>
                        )}
                      </div>
                    </Card>
                  );
                })}
              </div>
            )
          )}
        </div>
      </div>
    </div>
  );
};
