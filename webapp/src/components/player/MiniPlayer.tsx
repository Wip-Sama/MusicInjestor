import React from 'react';
import { Play, Pause, SkipForward } from 'lucide-react';
import { useAudioPlayer } from '../../context/AudioPlayerContext';

export const MiniPlayer: React.FC = () => {
  const {
    currentTrack,
    isPlaying,
    currentTime,
    duration,
    togglePlay,
    nextTrack,
    setIsPlayerOpen,
  } = useAudioPlayer();

  if (!currentTrack) return null;

  const progressPercent = duration > 0 ? (currentTime / duration) * 100 : 0;

  return (
    <div
      style={{
        position: 'fixed',
        bottom: 0,
        left: 0,
        right: 0,
        zIndex: 100,
        backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
        borderTop: '1px solid var(--tg-theme-section-separator-color, #38383a)',
        paddingBottom: 'calc(var(--tg-safe-area-inset-bottom, 0px) + 8px)',
        boxShadow: '0 -4px 16px rgba(0, 0, 0, 0.25)',
      }}
    >
      {/* Top micro progress line */}
      <div
        style={{
          width: '100%',
          height: '2px',
          backgroundColor: 'rgba(255, 255, 255, 0.1)',
        }}
      >
        <div
          style={{
            height: '100%',
            width: `${progressPercent}%`,
            backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
            transition: 'width 0.2s linear',
          }}
        />
      </div>

      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '8px 16px 4px 16px',
          maxWidth: '640px',
          margin: '0 auto',
        }}
      >
        {/* Track thumbnail and title (tap opens full player) */}
        <div
          onClick={() => setIsPlayerOpen(true)}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '12px',
            flex: 1,
            cursor: 'pointer',
            overflow: 'hidden',
          }}
        >
          <img
            src={currentTrack.cover_url}
            alt={currentTrack.album}
            style={{
              width: '46px',
              height: '46px',
              borderRadius: '8px',
              objectFit: 'cover',
              backgroundColor: '#1c1c1e',
              flexShrink: 0,
              boxShadow: '0 2px 6px rgba(0,0,0,0.3)',
            }}
            onError={(e) => {
              (e.currentTarget as HTMLImageElement).src =
                'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="46" height="46" fill="%233a3a3c"><rect width="46" height="46"/></svg>';
            }}
          />
          <div style={{ overflow: 'hidden', flex: 1, paddingRight: '8px' }}>
            <div
              style={{
                fontSize: '14px',
                fontWeight: 600,
                color: 'var(--tg-theme-text-color, #fff)',
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
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                marginTop: '2px',
              }}
            >
              {currentTrack.artist}
            </div>
          </div>
        </div>

        {/* Action buttons */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <button
            onClick={(e) => {
              e.stopPropagation();
              togglePlay();
            }}
            style={{
              background: 'var(--tg-theme-button-color, #0a84ff)',
              color: 'var(--tg-theme-button-text-color, #fff)',
              border: 'none',
              borderRadius: '50%',
              width: '38px',
              height: '38px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              cursor: 'pointer',
            }}
          >
            {isPlaying ? <Pause size={18} fill="currentColor" /> : <Play size={18} fill="currentColor" style={{ marginLeft: '2px' }} />}
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              nextTrack();
            }}
            style={{
              background: 'transparent',
              color: 'var(--tg-theme-text-color, #fff)',
              border: 'none',
              width: '36px',
              height: '36px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              cursor: 'pointer',
            }}
          >
            <SkipForward size={20} />
          </button>
        </div>
      </div>
    </div>
  );
};

