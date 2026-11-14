import React, { useEffect, useRef, useState } from 'react';
import { ChevronDown, Mic2 } from 'lucide-react';
import { useAudioPlayer } from '../../context/AudioPlayerContext';
import { apiFetch } from '../../lib/auth';
import { findActiveLrcIndex, parseLrc } from '../../lib/lrc';
import { fetchStreamEnrichment } from '../../lib/providerApi';
import { isStreamedTrack, LyricsResponse, ParsedLrcLine } from '../../types/music';

export const SyncedLyrics: React.FC = () => {
  const {
    currentTrack,
    currentTime,
    seekTo,
    isLyricsOpen,
    setIsLyricsOpen,
    getCachedStreamLyrics,
  } = useAudioPlayer();
  const [lyricsData, setLyricsData] = useState<LyricsResponse | null>(null);
  const [parsedLines, setParsedLines] = useState<ParsedLrcLine[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const activeLineRef = useRef<HTMLDivElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!currentTrack || !isLyricsOpen) return;
    let cancelled = false;

    const applyLyrics = (data: LyricsResponse) => {
      if (cancelled) return;
      setLyricsData(data);
      if (data.synced_lyrics) {
        setParsedLines(parseLrc(data.synced_lyrics));
      } else {
        setParsedLines([]);
      }
      setLoading(false);
    };

    if (isStreamedTrack(currentTrack)) {
      const cached = getCachedStreamLyrics(currentTrack.id);
      if (cached) {
        applyLyrics(cached);
        return () => {
          cancelled = true;
        };
      }
      setLoading(true);
      fetchStreamEnrichment({
        title: currentTrack.title,
        artist: currentTrack.artist,
        album: currentTrack.album,
        durationSeconds: currentTrack.duration_seconds,
      })
        .then((enriched) => {
          applyLyrics(
            enriched
              ? {
                  track_id: currentTrack.id,
                  has_synced: enriched.has_synced,
                  synced_lyrics: enriched.synced_lyrics,
                  plain_lyrics: enriched.plain_lyrics,
                }
              : {
                  track_id: currentTrack.id,
                  has_synced: false,
                  synced_lyrics: null,
                  plain_lyrics: null,
                }
          );
        })
        .catch((err) => {
          console.error('Failed to load streamed track lyrics:', err);
          if (!cancelled) setLoading(false);
        });
      return () => {
        cancelled = true;
      };
    }

    setLoading(true);
    apiFetch(`/api/v1/app/tracks/${currentTrack.id}/lyrics`)
      .then((res) => (res.ok ? res.json() : { has_synced: false, synced_lyrics: null, plain_lyrics: null }))
      .then((data: LyricsResponse) => applyLyrics(data))
      .catch((err) => {
        console.error('Failed to load lyrics:', err);
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [currentTrack?.id, currentTrack?.title, currentTrack?.artist, isLyricsOpen, getCachedStreamLyrics]);

  const activeIndex = parsedLines.length > 0 ? findActiveLrcIndex(parsedLines, currentTime) : -1;

  // Auto-scroll active line to vertical center smoothly
  useEffect(() => {
    if (activeLineRef.current && isLyricsOpen) {
      activeLineRef.current.scrollIntoView({
        behavior: 'smooth',
        block: 'center',
      });
    }
  }, [activeIndex, isLyricsOpen]);

  if (!isLyricsOpen || !currentTrack) return null;

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 300,
        backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
        display: 'flex',
        flexDirection: 'column',
        paddingTop: 'calc(var(--tg-safe-area-inset-top, 0px) + 12px)',
        paddingBottom: 'calc(var(--tg-safe-area-inset-bottom, 0px) + 16px)',
        animation: 'slideUp 0.25s ease-out',
      }}
    >
      {/* Top Header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '8px 20px',
          borderBottom: '1px solid var(--tg-theme-section-separator-color, #2c2c2e)',
        }}
      >
        <button
          onClick={() => setIsLyricsOpen(false)}
          style={{
            background: 'transparent',
            border: 'none',
            color: 'var(--tg-theme-text-color, #fff)',
            cursor: 'pointer',
            padding: '4px',
          }}
        >
          <ChevronDown size={28} />
        </button>
        <div style={{ textAlign: 'center', flex: 1, padding: '0 12px' }}>
          <div
            style={{
              fontSize: '15px',
              fontWeight: 700,
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {currentTrack.title}
          </div>
          <div
            style={{
              fontSize: '12px',
              color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '4px',
            }}
          >
            <Mic2 size={13} color="var(--tg-theme-button-color, #0a84ff)" />
            {lyricsData?.has_synced ? 'Synced Karaoke Lyrics' : 'Lyrics'}
          </div>
        </div>
        <div style={{ width: '28px' }} />
      </div>

      {/* Lyrics Content Container */}
      <div
        ref={containerRef}
        style={{
          flex: 1,
          overflowY: 'auto',
          padding: '24px 20px',
          textAlign: 'center',
          scrollBehavior: 'smooth',
        }}
      >
        {loading && (
          <div style={{ padding: '60px 0', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
            Loading lyrics...
          </div>
        )}

        {!loading && parsedLines.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '20px', paddingBottom: '120px' }}>
            {parsedLines.map((line, idx) => {
              const isActive = idx === activeIndex;
              return (
                <div
                  key={`${line.time}-${idx}`}
                  ref={isActive ? activeLineRef : null}
                  onClick={() => seekTo(line.time)}
                  style={{
                    fontSize: isActive ? '22px' : '17px',
                    fontWeight: isActive ? 700 : 500,
                    color: isActive ? 'var(--tg-theme-button-color, #0a84ff)' : 'rgba(255, 255, 255, 0.45)',
                    transform: isActive ? 'scale(1.05)' : 'scale(1)',
                    transition: 'all 0.2s cubic-bezier(0.2, 0.9, 0.3, 1)',
                    cursor: 'pointer',
                    padding: '6px 12px',
                    borderRadius: '8px',
                    userSelect: 'none',
                  }}
                >
                  {line.text || '♪'}
                </div>
              );
            })}
          </div>
        )}

        {!loading && parsedLines.length === 0 && lyricsData?.plain_lyrics && (
          <div
            style={{
              whiteSpace: 'pre-line',
              fontSize: '16px',
              lineHeight: '1.8',
              color: 'var(--tg-theme-text-color, #fff)',
              paddingBottom: '80px',
            }}
          >
            {lyricsData.plain_lyrics}
          </div>
        )}

        {!loading && parsedLines.length === 0 && !lyricsData?.plain_lyrics && (
          <div
            style={{
              padding: '80px 20px',
              color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
              fontSize: '15px',
            }}
          >
            No lyrics available for this track.
          </div>
        )}
      </div>
    </div>
  );
};

