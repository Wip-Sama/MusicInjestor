import React, { createContext, useContext, useEffect, useRef, useState } from 'react';
import { hapticFeedback } from '@telegram-apps/sdk-react';
import { getRawInitData } from '../lib/auth';
import { TrackItem } from '../types/music';

export type RepeatMode = 'off' | 'all' | 'one';

interface AudioPlayerContextType {
  currentTrack: TrackItem | null;
  isPlaying: boolean;
  currentTime: number;
  duration: number;
  buffered: number;
  queue: TrackItem[];
  queueIndex: number;
  repeatMode: RepeatMode;
  isShuffle: boolean;
  isPlayerOpen: boolean;
  isLyricsOpen: boolean;
  isQueueOpen: boolean;
  playTrack: (track: TrackItem, queueContext?: TrackItem[]) => void;
  togglePlay: () => void;
  seekTo: (seconds: number) => void;
  nextTrack: () => void;
  prevTrack: () => void;
  toggleRepeat: () => void;
  toggleShuffle: () => void;
  setIsPlayerOpen: (open: boolean) => void;
  setIsLyricsOpen: (open: boolean) => void;
  setIsQueueOpen: (open: boolean) => void;
  updateTrackData: (updated: TrackItem) => void;
}

const AudioPlayerContext = createContext<AudioPlayerContextType | null>(null);

export const AudioPlayerProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [currentTrack, setCurrentTrack] = useState<TrackItem | null>(null);
  const [isPlaying, setIsPlaying] = useState<boolean>(false);
  const [currentTime, setCurrentTime] = useState<number>(0);
  const [duration, setDuration] = useState<number>(0);
  const [buffered, setBuffered] = useState<number>(0);

  const [queue, setQueue] = useState<TrackItem[]>([]);
  const [queueIndex, setQueueIndex] = useState<number>(0);
  const [repeatMode, setRepeatMode] = useState<RepeatMode>('off');
  const [isShuffle, setIsShuffle] = useState<boolean>(false);

  const [isPlayerOpen, setIsPlayerOpen] = useState<boolean>(false);
  const [isLyricsOpen, setIsLyricsOpen] = useState<boolean>(false);
  const [isQueueOpen, setIsQueueOpen] = useState<boolean>(false);

  // Trigger haptic vibration if running inside Telegram
  const triggerHaptic = () => {
    try {
      if (hapticFeedback.impactOccurred.isAvailable()) {
        hapticFeedback.impactOccurred('light');
      }
    } catch {
      // Not in Telegram container
    }
  };

  // Initialize invisible HTML5 audio element
  useEffect(() => {
    const audio = new Audio();
    audio.preload = 'metadata';
    audioRef.current = audio;

    const handleTimeUpdate = () => {
      setCurrentTime(audio.currentTime);
      if (audio.buffered.length > 0) {
        setBuffered(audio.buffered.end(audio.buffered.length - 1));
      }
    };

    const handleLoadedMetadata = () => {
      setDuration(audio.duration || 0);
    };

    const handlePlay = () => setIsPlaying(true);
    const handlePause = () => setIsPlaying(false);

    const handleEnded = () => {
      // Audio track finished
      if (repeatMode === 'one') {
        audio.currentTime = 0;
        audio.play().catch(console.error);
      } else {
        nextTrack();
      }
    };

    const handleError = () => {
      const err = audio.error;
      console.error('Audio playback error encountered:', err?.code, err?.message);
      setIsPlaying(false);
    };

    audio.addEventListener('timeupdate', handleTimeUpdate);
    audio.addEventListener('loadedmetadata', handleLoadedMetadata);
    audio.addEventListener('play', handlePlay);
    audio.addEventListener('pause', handlePause);
    audio.addEventListener('ended', handleEnded);
    audio.addEventListener('error', handleError);

    return () => {
      audio.pause();
      audio.removeEventListener('timeupdate', handleTimeUpdate);
      audio.removeEventListener('loadedmetadata', handleLoadedMetadata);
      audio.removeEventListener('play', handlePlay);
      audio.removeEventListener('pause', handlePause);
      audio.removeEventListener('ended', handleEnded);
      audio.removeEventListener('error', handleError);
      audioRef.current = null;
    };
  }, [repeatMode]);

  // Hook into MediaSession API for lock-screen & notification controls
  useEffect(() => {
    if (!currentTrack || typeof window === 'undefined' || !('mediaSession' in navigator)) {
      return;
    }

    navigator.mediaSession.metadata = new MediaMetadata({
      title: currentTrack.title,
      artist: currentTrack.artist,
      album: currentTrack.album,
      artwork: [
        {
          src: currentTrack.cover_url,
          sizes: '512x512',
          type: 'image/jpeg',
        },
      ],
    });

    navigator.mediaSession.setActionHandler('play', () => {
      audioRef.current?.play().catch(console.error);
    });
    navigator.mediaSession.setActionHandler('pause', () => {
      audioRef.current?.pause();
    });
    navigator.mediaSession.setActionHandler('previoustrack', () => {
      prevTrack();
    });
    navigator.mediaSession.setActionHandler('nexttrack', () => {
      nextTrack();
    });
    navigator.mediaSession.setActionHandler('seekto', (details) => {
      if (details.seekTime !== undefined && audioRef.current) {
        audioRef.current.currentTime = details.seekTime;
      }
    });
  }, [currentTrack]);

  const playTrack = (track: TrackItem, queueContext?: TrackItem[]) => {
    triggerHaptic();
    const audio = audioRef.current;
    if (!audio) return;

    if (queueContext && queueContext.length > 0) {
      setQueue(queueContext);
      const idx = queueContext.findIndex((t) => t.id === track.id);
      setQueueIndex(idx >= 0 ? idx : 0);
    } else if (queue.length === 0) {
      setQueue([track]);
      setQueueIndex(0);
    }

    setCurrentTrack(track);
    const rawInit = getRawInitData();
    const sep = track.audio_url.includes('?') ? '&' : '?';
    const audioSrc = rawInit
      ? `${track.audio_url}${sep}tgWebAppData=${encodeURIComponent(rawInit)}`
      : track.audio_url;
    audio.src = audioSrc;
    audio.play().catch((err) => {
      console.warn('Audio playback failed or was prevented by browser policy:', err);
      setIsPlaying(false);
    });
  };

  const togglePlay = () => {
    triggerHaptic();
    const audio = audioRef.current;
    if (!audio || !currentTrack) return;

    if (audio.paused) {
      audio.play().catch(console.error);
    } else {
      audio.pause();
    }
  };

  const seekTo = (seconds: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = seconds;
    setCurrentTime(seconds);
  };

  const nextTrack = () => {
    triggerHaptic();
    if (queue.length === 0) return;

    let nextIdx: number;
    if (isShuffle) {
      nextIdx = Math.floor(Math.random() * queue.length);
    } else if (queueIndex + 1 < queue.length) {
      nextIdx = queueIndex + 1;
    } else if (repeatMode === 'all') {
      nextIdx = 0;
    } else {
      // Reached end of queue without repeat
      setIsPlaying(false);
      return;
    }

    const nextSong = queue[nextIdx];
    if (nextSong) {
      setQueueIndex(nextIdx);
      setCurrentTrack(nextSong);
      if (audioRef.current) {
        audioRef.current.src = nextSong.audio_url;
        audioRef.current.play().catch(console.error);
      }
    }
  };

  const prevTrack = () => {
    triggerHaptic();
    const audio = audioRef.current;
    if (!audio) return;

    // If song played for > 3 seconds, restart current track
    if (audio.currentTime > 3) {
      audio.currentTime = 0;
      setCurrentTime(0);
      return;
    }

    if (queue.length === 0) return;

    let prevIdx: number;
    if (queueIndex > 0) {
      prevIdx = queueIndex - 1;
    } else if (repeatMode === 'all') {
      prevIdx = queue.length - 1;
    } else {
      prevIdx = 0;
    }

    const prevSong = queue[prevIdx];
    if (prevSong) {
      setQueueIndex(prevIdx);
      setCurrentTrack(prevSong);
      audio.src = prevSong.audio_url;
      audio.play().catch(console.error);
    }
  };

  const toggleRepeat = () => {
    triggerHaptic();
    setRepeatMode((prev) => {
      if (prev === 'off') return 'all';
      if (prev === 'all') return 'one';
      return 'off';
    });
  };

  const toggleShuffle = () => {
    triggerHaptic();
    setIsShuffle((prev) => !prev);
  };

  const updateTrackData = (updated: TrackItem) => {
    if (currentTrack?.id === updated.id) {
      setCurrentTrack(updated);
    }
    setQueue((prevQueue) =>
      prevQueue.map((item) => (item.id === updated.id ? updated : item))
    );
  };

  return (
    <AudioPlayerContext.Provider
      value={{
        currentTrack,
        isPlaying,
        currentTime,
        duration,
        buffered,
        queue,
        queueIndex,
        repeatMode,
        isShuffle,
        isPlayerOpen,
        isLyricsOpen,
        isQueueOpen,
        playTrack,
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
      }}
    >
      {children}
    </AudioPlayerContext.Provider>
  );
};

export const useAudioPlayer = () => {
  const context = useContext(AudioPlayerContext);
  if (!context) {
    throw new Error('useAudioPlayer must be used within an AudioPlayerProvider');
  }
  return context;
};

