import React, { useEffect, useState } from 'react';
import { X, Play, Pause, ExternalLink, Disc, Send, Bot } from 'lucide-react';
import { useAudioPlayer } from '../../context/AudioPlayerContext';
import { apiFetch } from '../../lib/auth';
import { AlbumGapAnalysis, TrackItem } from '../../types/music';

interface AlbumGapModalProps {
  isOpen: boolean;
  onClose: () => void;
  albumId: string | null;
  artistName?: string;
  albumTitle?: string;
}

export const AlbumGapModal: React.FC<AlbumGapModalProps> = ({
  isOpen,
  onClose,
  albumId,
  artistName = '',
  albumTitle = '',
}) => {
  const {
    currentTrack,
    isPlaying,
    playTrack,
    openInTelegramBot,
    sendAlbumToChat,
    sendTrackToChat,
    openArtistPage,
  } = useAudioPlayer();
  const [gapData, setGapData] = useState<AlbumGapAnalysis | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [isSendingAlbum, setIsSendingAlbum] = useState<boolean>(false);
  const [albumSendStatus, setAlbumSendStatus] = useState<string | null>(null);
  const [sendingTrackId, setSendingTrackId] = useState<string | null>(null);

  const handleOpenAlbumInBot = () => {
    if (!albumId) return;
    openInTelegramBot(`album_${albumId}`);
  };

  const handleSendAlbumToChat = async () => {
    if (!gapData || isSendingAlbum) return;
    setIsSendingAlbum(true);
    setAlbumSendStatus('Sending...');
    const res = await sendAlbumToChat(gapData.album_id, gapData.artist, gapData.title);
    setAlbumSendStatus(res.ok ? 'Sent to chat!' : 'Failed');
    setTimeout(() => {
      setIsSendingAlbum(false);
      setAlbumSendStatus(null);
    }, 2500);
  };

  const handleSendTrackToChat = async (trackId: string) => {
    if (sendingTrackId) return;
    setSendingTrackId(trackId);
    await sendTrackToChat(trackId);
    setTimeout(() => {
      setSendingTrackId(null);
    }, 2000);
  };

  useEffect(() => {
    if (!isOpen || !albumId) {
      setGapData(null);
      return;
    }

    setLoading(true);
    setError(null);
    const params = new URLSearchParams();
    if (artistName) params.set('artist', artistName);
    if (albumTitle) params.set('album', albumTitle);

    apiFetch(`/api/v1/app/albums/${encodeURIComponent(albumId)}/gap-analysis?${params.toString()}`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP error ${res.status}`);
        return res.json();
      })
      .then((data: AlbumGapAnalysis) => {
        setGapData(data);
        setLoading(false);
      })
      .catch((err) => {
        console.error('Failed to load album gap analysis:', err);
        setError('Failed to load album coverage data.');
        setLoading(false);
      });
  }, [isOpen, albumId, artistName, albumTitle]);

  if (!isOpen) return null;

  const handlePlayPreview = (previewUrl: string, trackTitle: string, pos: number) => {
    const previewTrack: TrackItem = {
      id: `gap_preview:${albumId}:${pos}`,
      title: `${trackTitle} (Preview)`,
      artist: gapData?.artist || artistName || 'Unknown Artist',
      album: gapData?.title || albumTitle || 'Album Preview',
      year: null,
      track_number: pos,
      duration_seconds: 30,
      has_synced_lyrics: false,
      format: 'mp3',
      audio_url: previewUrl,
      cover_url: gapData?.cover_url || '',
    };
    playTrack(previewTrack);
  };

  const handlePlayLibraryTrack = (trackId: string, trackTitle: string, pos: number) => {
    apiFetch(`/api/v1/app/tracks?query=${encodeURIComponent(trackTitle)}`)
      .then((res) => res.json())
      .then((data) => {
        const found = (data.items || []).find((t: TrackItem) => t.id === trackId);
        if (found) {
          playTrack(found);
        } else {
          // Play with fallback direct audio URL
          const libTrack: TrackItem = {
            id: trackId,
            title: trackTitle,
            artist: gapData?.artist || artistName,
            album: gapData?.title || albumTitle,
            year: null,
            track_number: pos,
            duration_seconds: 0,
            has_synced_lyrics: false,
            format: 'original',
            audio_url: `/api/v1/app/tracks/${trackId}/audio`,
            cover_url: gapData?.cover_url || `/api/v1/app/tracks/${trackId}/cover`,
          };
          playTrack(libTrack);
        }
      })
      .catch(() => {});
  };

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        backgroundColor: 'rgba(0, 0, 0, 0.75)',
        backdropFilter: 'blur(10px)',
        zIndex: 9999,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '16px',
      }}
      onClick={onClose}
    >
      <div
        style={{
          width: '100%',
          maxWidth: '520px',
          maxHeight: '85vh',
          backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
          borderRadius: '16px',
          border: '1px solid rgba(255, 255, 255, 0.1)',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
          boxShadow: '0 20px 40px rgba(0,0,0,0.5)',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div
          style={{
            padding: '14px 16px',
            borderBottom: '1px solid rgba(255, 255, 255, 0.08)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Disc size={20} color="var(--tg-theme-button-color, #0a84ff)" />
            <span style={{ fontSize: '16px', fontWeight: 700, color: 'var(--tg-theme-text-color, #fff)' }}>
              Album Overview &amp; Gap Analysis
            </span>
          </div>
          <button
            onClick={onClose}
            style={{
              background: 'none',
              border: 'none',
              color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
              cursor: 'pointer',
              padding: '4px',
            }}
          >
            <X size={20} />
          </button>
        </div>

        {/* Body content */}
        <div style={{ padding: '16px', overflowY: 'auto', flex: 1, display: 'flex', flexDirection: 'column', gap: '14px' }}>
          {loading ? (
            <div style={{ textAlign: 'center', padding: '40px 0', color: '#8e8e93' }}>
              Analyzing album coverage and fetching track previews...
            </div>
          ) : error ? (
            <div style={{ textAlign: 'center', padding: '30px 0', color: '#ff453a' }}>
              {error}
            </div>
          ) : gapData ? (
            <>
              {/* Album Info Card */}
              <div
                style={{
                  display: 'flex',
                  gap: '12px',
                  backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                  borderRadius: '12px',
                  padding: '12px',
                  alignItems: 'center',
                }}
              >
                {gapData.cover_url ? (
                  <img
                    src={gapData.cover_url}
                    alt={gapData.title}
                    style={{ width: '64px', height: '64px', borderRadius: '8px', objectFit: 'cover', flexShrink: 0 }}
                  />
                ) : (
                  <div
                    style={{
                      width: '64px',
                      height: '64px',
                      borderRadius: '8px',
                      backgroundColor: 'rgba(255, 255, 255, 0.06)',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      flexShrink: 0,
                    }}
                  >
                    <Disc size={32} color="#8e8e93" />
                  </div>
                )}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: '15px', fontWeight: 700, color: '#fff', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {gapData.title}
                  </div>
                  <div
                    onClick={() => openArtistPage(gapData.artist)}
                    style={{
                      fontSize: '13px',
                      color: 'var(--tg-theme-button-color, #0a84ff)',
                      marginTop: '2px',
                      cursor: 'pointer',
                      textDecoration: 'underline',
                      textUnderlineOffset: '2px',
                    }}
                    title={`View ${gapData.artist} artist page`}
                  >
                    {gapData.artist}
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: '6px' }}>
                    <span
                      style={{
                        fontSize: '11px',
                        fontWeight: 700,
                        padding: '2px 6px',
                        borderRadius: '4px',
                        backgroundColor: gapData.is_complete ? 'rgba(48, 209, 88, 0.2)' : 'rgba(10, 132, 255, 0.2)',
                        color: gapData.is_complete ? '#30d158' : 'var(--tg-theme-button-color, #0a84ff)',
                      }}
                    >
                      {gapData.owned_tracks_count} / {gapData.total_tracks} Owned ({gapData.coverage_percent}%)
                    </span>
                    {gapData.missing_tracks_count > 0 && (
                      <span style={{ fontSize: '11px', color: '#ff9f0a' }}>
                        {gapData.missing_tracks_count} Missing
                      </span>
                    )}
                  </div>
                </div>
              </div>

              {/* Coverage Progress Bar */}
              <div style={{ width: '100%', height: '6px', borderRadius: '3px', backgroundColor: 'rgba(255, 255, 255, 0.08)', overflow: 'hidden' }}>
                <div
                  style={{
                    width: `${gapData.coverage_percent}%`,
                    height: '100%',
                    backgroundColor: gapData.is_complete ? '#30d158' : 'var(--tg-theme-button-color, #0a84ff)',
                    borderRadius: '3px',
                    transition: 'width 0.3s ease',
                  }}
                />
              </div>

              {/* Album Actions Row */}
              <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                {gapData.owned_tracks_count > 0 && (
                  <button
                    onClick={handleSendAlbumToChat}
                    disabled={isSendingAlbum}
                    title="Send all owned tracks from this album to Telegram chat"
                    style={{
                      flex: 1,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      gap: '6px',
                      padding: '8px 12px',
                      borderRadius: '8px',
                      border: 'none',
                      backgroundColor: albumSendStatus?.includes('Sent')
                        ? 'rgba(48, 209, 88, 0.2)'
                        : 'rgba(10, 132, 255, 0.15)',
                      color: albumSendStatus?.includes('Sent')
                        ? '#30d158'
                        : 'var(--tg-theme-button-color, #0a84ff)',
                      cursor: isSendingAlbum ? 'default' : 'pointer',
                      fontSize: '12px',
                      fontWeight: 600,
                      transition: 'all 0.15s ease',
                    }}
                  >
                    <Send size={14} />
                    <span>{albumSendStatus || `Send All Owned (${gapData.owned_tracks_count})`}</span>
                  </button>
                )}

                <button
                  onClick={handleOpenAlbumInBot}
                  title="Open this album in Telegram Bot"
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: '6px',
                    padding: '8px 12px',
                    borderRadius: '8px',
                    border: 'none',
                    backgroundColor: 'rgba(255, 255, 255, 0.08)',
                    color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                    cursor: 'pointer',
                    fontSize: '12px',
                    fontWeight: 600,
                  }}
                >
                  <Bot size={14} />
                  <span>Open in Bot</span>
                </button>
              </div>

              {/* Tracklist Gap Breakdown */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                <div style={{ fontSize: '12px', fontWeight: 700, textTransform: 'uppercase', color: '#8e8e93', letterSpacing: '0.5px' }}>
                  Album Tracklist
                </div>
                {gapData.tracks.map((trk) => {
                  const isTrkPlaying = currentTrack?.id === `gap_preview:${albumId}:${trk.position}` && isPlaying;
                  const isLibPlaying = trk.library_track_id && currentTrack?.id === trk.library_track_id && isPlaying;

                  return (
                    <div
                      key={trk.position}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        padding: '8px 10px',
                        borderRadius: '8px',
                        backgroundColor: isTrkPlaying || isLibPlaying ? 'rgba(10, 132, 255, 0.15)' : 'rgba(255, 255, 255, 0.04)',
                        border: '1px solid rgba(255, 255, 255, 0.04)',
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0 }}>
                        <span style={{ fontSize: '12px', color: '#8e8e93', width: '20px', flexShrink: 0 }}>
                          #{trk.position}
                        </span>
                        <div style={{ minWidth: 0 }}>
                          <div style={{ fontSize: '13px', fontWeight: 600, color: '#fff', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                            {trk.title}
                          </div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginTop: '2px' }}>
                            {trk.is_owned ? (
                              <span style={{ fontSize: '10px', color: '#30d158', backgroundColor: 'rgba(48, 209, 88, 0.15)', padding: '1px 5px', borderRadius: '4px' }}>
                                In Library
                              </span>
                            ) : (
                              <span style={{ fontSize: '10px', color: '#ff9f0a', backgroundColor: 'rgba(255, 159, 10, 0.15)', padding: '1px 5px', borderRadius: '4px' }}>
                                Missing
                              </span>
                            )}
                          </div>
                        </div>
                      </div>

                      {/* Play & Send Action Buttons */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                        {trk.is_owned && trk.library_track_id ? (
                          <>
                            <button
                              onClick={() => handlePlayLibraryTrack(trk.library_track_id!, trk.title, trk.position)}
                              style={{
                                display: 'flex',
                                alignItems: 'center',
                                gap: '4px',
                                padding: '5px 8px',
                                borderRadius: '6px',
                                border: 'none',
                                backgroundColor: isLibPlaying ? '#30d158' : 'rgba(48, 209, 88, 0.18)',
                                color: isLibPlaying ? '#fff' : '#30d158',
                                cursor: 'pointer',
                                fontSize: '11px',
                                fontWeight: 600,
                              }}
                            >
                              {isLibPlaying ? <Pause size={12} /> : <Play size={12} />}
                              <span>{isLibPlaying ? 'Playing' : 'Play Full'}</span>
                            </button>

                            <button
                              onClick={() => handleSendTrackToChat(trk.library_track_id!)}
                              title="Send audio file to Telegram chat"
                              style={{
                                display: 'flex',
                                alignItems: 'center',
                                padding: '5px 7px',
                                borderRadius: '6px',
                                border: 'none',
                                backgroundColor: sendingTrackId === trk.library_track_id ? '#30d158' : 'rgba(255, 255, 255, 0.08)',
                                color: sendingTrackId === trk.library_track_id ? '#fff' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                                cursor: 'pointer',
                              }}
                            >
                              <Send size={12} />
                            </button>
                          </>
                        ) : trk.preview_url ? (
                          <button
                            onClick={() => handlePlayPreview(trk.preview_url!, trk.title, trk.position)}
                            style={{
                              display: 'flex',
                              alignItems: 'center',
                              gap: '4px',
                              padding: '5px 8px',
                              borderRadius: '6px',
                              border: 'none',
                              backgroundColor: isTrkPlaying ? 'var(--tg-theme-button-color, #0a84ff)' : 'rgba(255, 255, 255, 0.08)',
                              color: '#fff',
                              cursor: 'pointer',
                              fontSize: '11px',
                              fontWeight: 600,
                            }}
                          >
                            {isTrkPlaying ? <Pause size={12} /> : <Play size={12} />}
                            <span>{isTrkPlaying ? 'Playing' : '30s Preview'}</span>
                          </button>
                        ) : (
                          <span style={{ fontSize: '11px', color: '#8e8e93' }}>No preview</span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>

              {/* External Provider Link */}
              {gapData.external_url && (
                <div style={{ display: 'flex', justifyContent: 'flex-end', paddingTop: '6px' }}>
                  <a
                    href={gapData.external_url}
                    target="_blank"
                    rel="noreferrer"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '4px',
                      color: 'var(--tg-theme-button-color, #0a84ff)',
                      fontSize: '12px',
                      textDecoration: 'none',
                    }}
                  >
                    <span>View on Deezer</span>
                    <ExternalLink size={12} />
                  </a>
                </div>
              )}
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
};
