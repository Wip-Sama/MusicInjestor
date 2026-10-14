import React from 'react';
import { Play, Volume2, Sliders } from 'lucide-react';
import { useAudioPlayer } from '../../context/AudioPlayerContext';
import { formatTime } from '../../lib/lrc';
import { TrackItem } from '../../types/music';

interface TrackCardProps {
  track: TrackItem;
  allTracks: TrackItem[];
  onEdit?: (track: TrackItem) => void;
}

export const TrackCard: React.FC<TrackCardProps> = ({ track, allTracks, onEdit }) => {
  const { currentTrack, isPlaying, playTrack, togglePlay } = useAudioPlayer();
  const isCurrent = currentTrack?.id === track.id;

  const handleClick = () => {
    if (isCurrent) {
      togglePlay();
    } else {
      playTrack(track, allTracks);
    }
  };

  const handleEditClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    onEdit?.(track);
  };

  return (
    <div
      onClick={handleClick}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '12px',
        padding: '10px 14px',
        borderRadius: '12px',
        backgroundColor: isCurrent ? 'var(--tg-theme-secondary-bg-color, #2c2c2e)' : 'transparent',
        border: isCurrent ? '1px solid var(--tg-theme-button-color, #0a84ff)' : '1px solid transparent',
        cursor: 'pointer',
        transition: 'background-color 0.15s ease',
      }}
    >
      {/* Cover Image Thumbnail with play overlay */}
      <div
        style={{
          position: 'relative',
          width: '50px',
          height: '50px',
          borderRadius: '8px',
          overflow: 'hidden',
          backgroundColor: '#2c2c2e',
          flexShrink: 0,
        }}
      >
        <img
          src={track.cover_url}
          alt={track.album}
          style={{
            width: '100%',
            height: '100%',
            objectFit: 'cover',
          }}
          onError={(e) => {
            (e.currentTarget as HTMLImageElement).src =
              'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="50" height="50" fill="%233a3a3c"><rect width="50" height="50"/></svg>';
          }}
        />

        {/* Playing indicator badge */}
        {isCurrent && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              backgroundColor: 'rgba(0, 0, 0, 0.45)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'var(--tg-theme-button-color, #0a84ff)',
            }}
          >
            {isPlaying ? <Volume2 size={20} /> : <Play size={20} fill="currentColor" />}
          </div>
        )}
      </div>

      {/* Track Info & Metadata */}
      <div style={{ flex: 1, overflow: 'hidden' }}>
        <div
          style={{
            fontSize: '15px',
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
            fontSize: '13px',
            color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            marginTop: '2px',
          }}
        >
          {track.artist} • {track.album}
        </div>
      </div>

      {/* Badges, Duration & Studio Edit Button */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          flexShrink: 0,
        }}
      >
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'flex-end',
            gap: '4px',
          }}
        >
          <div
            style={{
              fontSize: '12px',
              fontWeight: 500,
              color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
            }}
          >
            {formatTime(track.duration_seconds)}
          </div>

          <div style={{ display: 'flex', gap: '4px' }}>
            {track.has_synced_lyrics && (
              <span
                style={{
                  fontSize: '10px',
                  fontWeight: 700,
                  backgroundColor: 'rgba(10, 132, 255, 0.15)',
                  color: 'var(--tg-theme-button-color, #0a84ff)',
                  padding: '2px 5px',
                  borderRadius: '4px',
                }}
              >
                LRC
              </span>
            )}
            <span
              style={{
                fontSize: '10px',
                fontWeight: 700,
                textTransform: 'uppercase',
                backgroundColor: 'rgba(255, 255, 255, 0.08)',
                color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                padding: '2px 5px',
                borderRadius: '4px',
              }}
            >
              {track.format}
            </span>
          </div>
        </div>

        {onEdit && (
          <button
            onClick={handleEditClick}
            title="Edit tags, cover & lyrics in Studio"
            style={{
              background: 'rgba(255, 255, 255, 0.06)',
              border: 'none',
              borderRadius: '8px',
              padding: '6px',
              color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <Sliders size={16} />
          </button>
        )}
      </div>
    </div>
  );
};

