import React from 'react';
import { ChevronDown, Music, Play } from 'lucide-react';
import { useAudioPlayer } from '../../context/AudioPlayerContext';
import { formatTime } from '../../lib/lrc';

export const QueueDrawer: React.FC = () => {
  const { queue, queueIndex, isQueueOpen, setIsQueueOpen, playTrack } = useAudioPlayer();

  if (!isQueueOpen) return null;

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
      {/* Header */}
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
          onClick={() => setIsQueueOpen(false)}
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
        <div style={{ fontSize: '16px', fontWeight: 700 }}>
          Up Next ({queue.length})
        </div>
        <div style={{ width: '28px' }} />
      </div>

      {/* Queue items list */}
      <div
        style={{
          flex: 1,
          overflowY: 'auto',
          padding: '12px 16px',
          display: 'flex',
          flexDirection: 'column',
          gap: '8px',
        }}
      >
        {queue.map((track, idx) => {
          const isCurrent = idx === queueIndex;
          return (
            <div
              key={`${track.id}-${idx}`}
              onClick={() => playTrack(track, queue)}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '12px',
                padding: '10px 12px',
                borderRadius: '10px',
                backgroundColor: isCurrent ? 'var(--tg-theme-secondary-bg-color, #2c2c2e)' : 'transparent',
                border: isCurrent ? '1px solid var(--tg-theme-button-color, #0a84ff)' : '1px solid transparent',
                cursor: 'pointer',
              }}
            >
              <div
                style={{
                  width: '24px',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  color: isCurrent ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                  fontSize: '13px',
                  fontWeight: 600,
                }}
              >
                {isCurrent ? <Play size={14} fill="currentColor" /> : idx + 1}
              </div>

              <img
                src={track.cover_url}
                alt={track.title}
                style={{
                  width: '40px',
                  height: '40px',
                  borderRadius: '6px',
                  objectFit: 'cover',
                  backgroundColor: '#2c2c2e',
                }}
                onError={(e) => {
                  (e.currentTarget as HTMLImageElement).src =
                    'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" fill="%233a3a3c"><rect width="40" height="40"/></svg>';
                }}
              />

              <div style={{ flex: 1, overflow: 'hidden' }}>
                <div
                  style={{
                    fontSize: '14px',
                    fontWeight: 600,
                    color: isCurrent ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-text-color, #fff)',
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}
                >
                  {track.title}
                </div>
                <div
                  style={{
                    fontSize: '12px',
                    color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}
                >
                  {track.artist}
                </div>
              </div>

              <div
                style={{
                  fontSize: '12px',
                  color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                }}
              >
                {formatTime(track.duration_seconds)}
              </div>
            </div>
          );
        })}

        {queue.length === 0 && (
          <div
            style={{
              padding: '60px 20px',
              textAlign: 'center',
              color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
            }}
          >
            <Music size={32} style={{ marginBottom: '8px', opacity: 0.5 }} />
            <div>Queue is empty.</div>
          </div>
        )}
      </div>
    </div>
  );
};

