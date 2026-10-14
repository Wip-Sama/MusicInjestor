import React, { useState } from 'react';
import {
  ChevronDown,
  Play,
  Pause,
  SkipBack,
  SkipForward,
  Shuffle,
  Repeat,
  Repeat1,
  Mic2,
  ListMusic,
  Sliders,
} from 'lucide-react';
import { useAudioPlayer } from '../../context/AudioPlayerContext';
import { formatTime } from '../../lib/lrc';
import { SongStudioModal } from '../studio/SongStudioModal';

export const FullscreenPlayer: React.FC = () => {
  const [isStudioOpen, setIsStudioOpen] = useState<boolean>(false);
  const {
    currentTrack,
    isPlaying,
    currentTime,
    duration,
    repeatMode,
    isShuffle,
    isPlayerOpen,
    togglePlay,
    seekTo,
    nextTrack,
    prevTrack,
    toggleRepeat,
    toggleShuffle,
    setIsPlayerOpen,
    setIsLyricsOpen,
    setIsQueueOpen,
    updateTrackData,
  } = useAudioPlayer();

  if (!isPlayerOpen || !currentTrack) return null;

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 200,
        backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'space-between',
        paddingTop: 'calc(var(--tg-safe-area-inset-top, 0px) + 12px)',
        paddingBottom: 'calc(var(--tg-safe-area-inset-bottom, 0px) + 24px)',
        paddingLeft: '24px',
        paddingRight: '24px',
        animation: 'slideUp 0.25s ease-out',
      }}
    >
      {/* Top Header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          height: '44px',
        }}
      >
        <button
          onClick={() => setIsPlayerOpen(false)}
          style={{
            background: 'transparent',
            border: 'none',
            color: 'var(--tg-theme-text-color, #fff)',
            cursor: 'pointer',
            padding: '8px',
          }}
        >
          <ChevronDown size={28} />
        </button>

        <div
          style={{
            textAlign: 'center',
            fontSize: '13px',
            fontWeight: 600,
            color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
            textTransform: 'uppercase',
            letterSpacing: '0.5px',
            maxWidth: '220px',
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {currentTrack.album}
        </div>

        <button
          onClick={() => setIsQueueOpen(true)}
          style={{
            background: 'transparent',
            border: 'none',
            color: 'var(--tg-theme-text-color, #fff)',
            cursor: 'pointer',
            padding: '8px',
          }}
        >
          <ListMusic size={24} />
        </button>
      </div>

      {/* Middle: Album Cover Art */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flex: 1,
          padding: '16px 0',
        }}
      >
        <div
          style={{
            width: '100%',
            maxWidth: '320px',
            aspectRatio: '1 / 1',
            borderRadius: '20px',
            overflow: 'hidden',
            boxShadow: '0 16px 40px rgba(0, 0, 0, 0.5)',
            backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
          }}
        >
          <img
            src={currentTrack.cover_url}
            alt={currentTrack.album}
            style={{
              width: '100%',
              height: '100%',
              objectFit: 'cover',
            }}
            onError={(e) => {
              (e.currentTarget as HTMLImageElement).src =
                'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" fill="%233a3a3c"><rect width="300" height="300"/></svg>';
            }}
          />
        </div>
      </div>

      {/* Track Info */}
      <div style={{ marginBottom: '16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div style={{ flex: 1, overflow: 'hidden' }}>
            <div
              style={{
                fontSize: '22px',
                fontWeight: 700,
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
                fontSize: '16px',
                fontWeight: 500,
                color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                marginTop: '4px',
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
            >
              {currentTrack.artist}
            </div>
          </div>

          <div
            style={{
              fontSize: '11px',
              fontWeight: 700,
              textTransform: 'uppercase',
              backgroundColor: 'rgba(255, 255, 255, 0.1)',
              color: 'var(--tg-theme-button-color, #0a84ff)',
              padding: '4px 8px',
              borderRadius: '6px',
              marginLeft: '12px',
            }}
          >
            {currentTrack.format}
          </div>
        </div>
      </div>

      {/* Scrubber Seekbar */}
      <div style={{ marginBottom: '16px' }}>
        <input
          type="range"
          min={0}
          max={duration || 100}
          value={currentTime}
          onChange={(e) => seekTo(parseFloat(e.target.value))}
          style={{
            width: '100%',
            accentColor: 'var(--tg-theme-button-color, #0a84ff)',
            cursor: 'pointer',
          }}
        />
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            fontSize: '12px',
            color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
            marginTop: '4px',
          }}
        >
          <span>{formatTime(currentTime)}</span>
          <span>{formatTime(duration)}</span>
        </div>
      </div>

      {/* Core Playback Controls */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: '20px',
        }}
      >
        <button
          onClick={toggleShuffle}
          style={{
            background: 'transparent',
            border: 'none',
            color: isShuffle ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
            cursor: 'pointer',
            padding: '8px',
          }}
        >
          <Shuffle size={20} />
        </button>

        <button
          onClick={prevTrack}
          style={{
            background: 'transparent',
            border: 'none',
            color: 'var(--tg-theme-text-color, #fff)',
            cursor: 'pointer',
            padding: '8px',
          }}
        >
          <SkipBack size={30} fill="currentColor" />
        </button>

        <button
          onClick={togglePlay}
          style={{
            width: '68px',
            height: '68px',
            borderRadius: '50%',
            backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
            color: 'var(--tg-theme-button-text-color, #fff)',
            border: 'none',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            cursor: 'pointer',
            boxShadow: '0 8px 24px rgba(10, 132, 255, 0.4)',
          }}
        >
          {isPlaying ? (
            <Pause size={30} fill="currentColor" />
          ) : (
            <Play size={30} fill="currentColor" style={{ marginLeft: '4px' }} />
          )}
        </button>

        <button
          onClick={nextTrack}
          style={{
            background: 'transparent',
            border: 'none',
            color: 'var(--tg-theme-text-color, #fff)',
            cursor: 'pointer',
            padding: '8px',
          }}
        >
          <SkipForward size={30} fill="currentColor" />
        </button>

        <button
          onClick={toggleRepeat}
          style={{
            background: 'transparent',
            border: 'none',
            color: repeatMode !== 'off' ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
            cursor: 'pointer',
            padding: '8px',
          }}
        >
          {repeatMode === 'one' ? <Repeat1 size={20} /> : <Repeat size={20} />}
        </button>
      </div>

      {/* Bottom Features Toggle Bar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '24px',
          paddingTop: '8px',
        }}
      >
        <button
          onClick={() => setIsLyricsOpen(true)}
          style={{
            background: 'rgba(255, 255, 255, 0.08)',
            border: 'none',
            color: currentTrack.has_synced_lyrics ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-text-color, #fff)',
            borderRadius: '20px',
            padding: '8px 18px',
            fontSize: '13px',
            fontWeight: 600,
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
            cursor: 'pointer',
          }}
        >
          <Mic2 size={16} />
          <span>Lyrics</span>
        </button>

        <button
          onClick={() => setIsQueueOpen(true)}
          style={{
            background: 'rgba(255, 255, 255, 0.08)',
            border: 'none',
            color: 'var(--tg-theme-text-color, #fff)',
            borderRadius: '20px',
            padding: '8px 18px',
            fontSize: '13px',
            fontWeight: 600,
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
            cursor: 'pointer',
          }}
        >
          <ListMusic size={16} />
          <span>Queue</span>
        </button>

        <button
          onClick={() => setIsStudioOpen(true)}
          style={{
            background: 'rgba(255, 255, 255, 0.08)',
            border: 'none',
            color: 'var(--tg-theme-text-color, #fff)',
            borderRadius: '20px',
            padding: '8px 18px',
            fontSize: '13px',
            fontWeight: 600,
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
            cursor: 'pointer',
          }}
        >
          <Sliders size={16} />
          <span>Studio</span>
        </button>
      </div>

      {/* Song Studio Modal */}
      <SongStudioModal
        isOpen={isStudioOpen}
        track={currentTrack}
        onClose={() => setIsStudioOpen(false)}
        onTrackUpdated={updateTrackData}
      />
    </div>
  );
};

