import React, { useEffect, useState } from 'react';
import {
  Clock,
  Plus,
  Trash2,
  FileText,
  ListOrdered,
  Check,
  Loader2,
  AlertCircle,
  PlayCircle,
  Sparkles,
  Search,
  Mic2,
} from 'lucide-react';
import { useAudioPlayer } from '../../context/AudioPlayerContext';
import { apiFetch } from '../../lib/auth';
import { formatTime, parseLrc } from '../../lib/lrc';
import { LyricsResponse, ParsedLrcLine, TrackItem } from '../../types/music';
import { AutoLyricsResponse, ProposalResponse } from '../../types/studio';

interface LrcEditorModalProps {
  track: TrackItem;
  onPropose: (proposal: ProposalResponse) => void;
  isProposing: boolean;
  onCancel: () => void;
}

export const LrcEditorModal: React.FC<LrcEditorModalProps> = ({
  track,
  onPropose,
  isProposing,
  onCancel,
}) => {
  const { currentTrack, currentTime } = useAudioPlayer();
  const isCurrentlyPlayingThisTrack = currentTrack?.id === track.id;

  const [mode, setMode] = useState<'lines' | 'raw'>('lines');
  const [lines, setLines] = useState<ParsedLrcLine[]>([]);
  const [rawText, setRawText] = useState<string>('');
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // Auto-Search Lyrics states
  const [isSearchingLyrics, setIsSearchingLyrics] = useState<boolean>(false);
  const [lyricsSearchQuery, setLyricsSearchQuery] = useState<string>('');
  const [lyricsSearchResult, setLyricsSearchResult] = useState<AutoLyricsResponse | null>(null);
  const [lyricsSearchError, setLyricsSearchError] = useState<string | null>(null);

  // Load existing lyrics on mount
  useEffect(() => {
    setLoading(true);
    apiFetch(`/api/v1/app/tracks/${track.id}/lyrics`)
      .then((res) => res.json())
      .then((data: LyricsResponse) => {
        const text = data.synced_lyrics || data.plain_lyrics || '';
        setRawText(text);
        if (text) {
          const parsed = parseLrc(text);
          if (parsed.length > 0) {
            setLines(parsed);
          } else {
            // plain text lines without timestamps
            setLines(
              text
                .split('\n')
                .map((l, idx) => ({ time: idx * 5, text: l.trim() }))
                .filter((l) => l.text)
            );
          }
        }
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message || 'Failed to fetch existing lyrics');
        setLoading(false);
      });
  }, [track.id]);

  // Synchronize rawText when switching from lines to raw
  const switchToRaw = () => {
    const formatted = lines
      .map((l) => {
        const mins = Math.floor(l.time / 60);
        const secs = Math.floor(l.time % 60);
        const ms = Math.floor((l.time % 1) * 100);
        const ts = `[${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}.${String(ms).padStart(2, '0')}]`;
        return `${ts} ${l.text}`;
      })
      .join('\n');
    setRawText(formatted);
    setMode('raw');
  };

  // Synchronize lines when switching from raw to lines
  const switchToLines = () => {
    const parsed = parseLrc(rawText);
    if (parsed.length > 0) {
      setLines(parsed);
    } else {
      setLines(
        rawText
          .split('\n')
          .map((l, idx) => ({ time: idx * 5, text: l.trim() }))
          .filter((l) => l.text)
      );
    }
    setMode('lines');
  };

  // Adjust all timestamps by offset (+0.5s or -0.5s)
  const adjustTimingOffset = (offsetSeconds: number) => {
    setLines((prev) =>
      prev.map((l) => ({
        ...l,
        time: Math.max(0, parseFloat((l.time + offsetSeconds).toFixed(2))),
      }))
    );
  };

  // Stamp current player time into a specific line
  const stampCurrentTime = (index: number) => {
    if (!isCurrentlyPlayingThisTrack) return;
    setLines((prev) => {
      const next = [...prev];
      next[index] = { ...next[index], time: parseFloat(currentTime.toFixed(2)) };
      return next.sort((a, b) => a.time - b.time);
    });
  };

  const handleLineTextChange = (index: number, val: string) => {
    setLines((prev) => {
      const next = [...prev];
      next[index] = { ...next[index], text: val };
      return next;
    });
  };

  const addLine = () => {
    const lastTime = lines.length > 0 ? lines[lines.length - 1].time + 3 : 0;
    setLines((prev) => [...prev, { time: lastTime, text: '' }]);
  };

  const deleteLine = (index: number) => {
    setLines((prev) => prev.filter((_, i) => i !== index));
  };

  const handleReviewLyrics = async () => {
    let finalLrc = '';
    if (mode === 'raw') {
      finalLrc = rawText.trim();
    } else {
      finalLrc = lines
        .map((l) => {
          const mins = Math.floor(l.time / 60);
          const secs = Math.floor(l.time % 60);
          const ms = Math.floor((l.time % 1) * 100);
          const ts = `[${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}.${String(ms).padStart(2, '0')}]`;
          return `${ts} ${l.text}`;
        })
        .join('\n');
    }

    try {
      const propRes = await apiFetch(`/api/v1/app/tracks/${track.id}/propose-edit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action_type: 'lyrics',
          lyrics: finalLrc,
          year: track.year,
        }),
      });

      if (!propRes.ok) {
        throw new Error(`Proposal failed (${propRes.status})`);
      }

      const proposal: ProposalResponse = await propRes.json();
      onPropose(proposal);
    } catch (err: unknown) {
      setError((err as Error).message || 'Failed to prepare lyrics preview');
    }
  };

  const handleAutoSearchLyrics = async () => {
    setIsSearchingLyrics(true);
    setLyricsSearchError(null);
    setLyricsSearchResult(null);
    try {
      const res = await apiFetch(`/api/v1/app/tracks/${track.id}/auto-lyrics`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: lyricsSearchQuery.trim() }),
      });
      if (!res.ok) {
        throw new Error(`Lyrics search failed (${res.status})`);
      }
      const data: AutoLyricsResponse = await res.json();
      setLyricsSearchResult(data);
      if (!data.found || !data.lyrics) {
        setLyricsSearchError(data.message || 'No lyrics found on LRCLIB.');
      }
    } catch (err: unknown) {
      setLyricsSearchError((err as Error).message || 'Failed to auto-search lyrics');
    } finally {
      setIsSearchingLyrics(false);
    }
  };

  const loadFetchedLyricsIntoEditor = (text: string) => {
    setRawText(text);
    const parsed = parseLrc(text);
    if (parsed.length > 0) {
      setLines(parsed);
      setMode('lines');
    } else {
      setLines(
        text
          .split('\n')
          .map((l, idx) => ({ time: idx * 5, text: l.trim() }))
          .filter((l) => l.text)
      );
      setMode('lines');
    }
    setLyricsSearchResult(null);
  };

  const handleDirectProposeLyrics = async (lyricsText: string) => {
    try {
      const propRes = await apiFetch(`/api/v1/app/tracks/${track.id}/propose-edit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action_type: 'lyrics',
          lyrics: lyricsText.trim(),
          year: track.year,
        }),
      });
      if (!propRes.ok) {
        throw new Error(`Proposal failed (${propRes.status})`);
      }
      const proposal: ProposalResponse = await propRes.json();
      onPropose(proposal);
    } catch (err: unknown) {
      setError((err as Error).message || 'Failed to prepare lyrics preview');
    }
  };

  if (loading) {
    return (
      <div style={{ padding: '40px', textAlign: 'center', color: '#8e8e93' }}>
        <Loader2 size={24} className="animate-spin" style={{ margin: '0 auto 8px auto' }} />
        <div>Loading existing lyrics...</div>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
      {/* Auto-Search Lyrics Toolbar */}
      <div
        style={{
          backgroundColor: 'rgba(10, 132, 255, 0.08)',
          border: '1px solid rgba(10, 132, 255, 0.2)',
          borderRadius: '12px',
          padding: '12px',
          display: 'flex',
          flexDirection: 'column',
          gap: '10px',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', fontWeight: 600 }}>
            <Sparkles size={16} color="var(--tg-theme-button-color, #0a84ff)" />
            <span>Auto-Search Lyrics from LRCLIB</span>
          </div>
          <button
            onClick={() => handleAutoSearchLyrics()}
            disabled={isSearchingLyrics}
            style={{
              backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
              color: '#fff',
              border: 'none',
              borderRadius: '8px',
              padding: '6px 12px',
              fontSize: '12px',
              fontWeight: 600,
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              cursor: isSearchingLyrics ? 'not-allowed' : 'pointer',
              opacity: isSearchingLyrics ? 0.7 : 1,
            }}
          >
            {isSearchingLyrics ? <Loader2 size={13} className="animate-spin" /> : <Mic2 size={13} />}
            <span>Auto-Search</span>
          </button>
        </div>

        <div style={{ display: 'flex', gap: '8px' }}>
          <div
            style={{
              flex: 1,
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
              border: '1px solid var(--tg-theme-secondary-bg-color, #3a3a3c)',
              borderRadius: '8px',
              padding: '0 10px',
            }}
          >
            <Search size={14} color="#8e8e93" />
            <input
              type="text"
              placeholder={`${track.artist} - ${track.title} (or custom query)`}
              value={lyricsSearchQuery}
              onChange={(e) => setLyricsSearchQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleAutoSearchLyrics()}
              style={{
                flex: 1,
                border: 'none',
                background: 'transparent',
                color: 'var(--tg-theme-text-color, #fff)',
                fontSize: '12px',
                padding: '8px 0',
                outline: 'none',
              }}
            />
          </div>
        </div>

        {lyricsSearchError && (
          <div
            style={{
              fontSize: '12px',
              color: '#ff453a',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            <AlertCircle size={14} />
            <span>{lyricsSearchError}</span>
          </div>
        )}

        {lyricsSearchResult?.found && lyricsSearchResult.lyrics && (
          <div
            style={{
              backgroundColor: 'rgba(48, 209, 88, 0.1)',
              border: '1px solid rgba(48, 209, 88, 0.3)',
              borderRadius: '8px',
              padding: '10px 12px',
              display: 'flex',
              flexDirection: 'column',
              gap: '8px',
            }}
          >
            <div style={{ fontSize: '12px', color: '#30d158', fontWeight: 600 }}>
              ✓ Found {lyricsSearchResult.synced ? 'Synced LRC' : 'Plain Text'} lyrics ({lyricsSearchResult.line_count} lines)
            </div>
            <div style={{ display: 'flex', gap: '8px' }}>
              <button
                onClick={() => loadFetchedLyricsIntoEditor(lyricsSearchResult.lyrics!)}
                style={{
                  flex: 1,
                  padding: '6px 10px',
                  borderRadius: '6px',
                  border: '1px solid rgba(48, 209, 88, 0.4)',
                  backgroundColor: 'transparent',
                  color: '#30d158',
                  fontSize: '12px',
                  fontWeight: 600,
                  cursor: 'pointer',
                }}
              >
                Load into Editor
              </button>
              <button
                onClick={() => handleDirectProposeLyrics(lyricsSearchResult.lyrics!)}
                style={{
                  flex: 1,
                  padding: '6px 10px',
                  borderRadius: '6px',
                  border: 'none',
                  backgroundColor: '#30d158',
                  color: '#000',
                  fontSize: '12px',
                  fontWeight: 700,
                  cursor: 'pointer',
                }}
              >
                Review &amp; Propose
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Top Mode Toggle & Player Timing Banner */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div
          style={{
            display: 'flex',
            backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
            borderRadius: '8px',
            padding: '2px',
          }}
        >
          <button
            onClick={() => mode === 'raw' && switchToLines()}
            style={{
              padding: '6px 12px',
              borderRadius: '6px',
              border: 'none',
              backgroundColor: mode === 'lines' ? 'var(--tg-theme-button-color, #0a84ff)' : 'transparent',
              color: '#fff',
              fontSize: '12px',
              fontWeight: 600,
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
              cursor: 'pointer',
            }}
          >
            <ListOrdered size={14} />
            <span>Synced Lines</span>
          </button>
          <button
            onClick={() => mode === 'lines' && switchToRaw()}
            style={{
              padding: '6px 12px',
              borderRadius: '6px',
              border: 'none',
              backgroundColor: mode === 'raw' ? 'var(--tg-theme-button-color, #0a84ff)' : 'transparent',
              color: '#fff',
              fontSize: '12px',
              fontWeight: 600,
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
              cursor: 'pointer',
            }}
          >
            <FileText size={14} />
            <span>Raw Text</span>
          </button>
        </div>

        {/* Global Nudge Controls */}
        {mode === 'lines' && (
          <div style={{ display: 'flex', gap: '4px' }}>
            <button
              onClick={() => adjustTimingOffset(-0.5)}
              title="Shift all timestamps back by 0.5s"
              style={{
                backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                border: '1px solid #3a3a3c',
                borderRadius: '6px',
                padding: '4px 8px',
                color: '#fff',
                fontSize: '11px',
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              -0.5s
            </button>
            <button
              onClick={() => adjustTimingOffset(0.5)}
              title="Shift all timestamps forward by 0.5s"
              style={{
                backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                border: '1px solid #3a3a3c',
                borderRadius: '6px',
                padding: '4px 8px',
                color: '#fff',
                fontSize: '11px',
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              +0.5s
            </button>
          </div>
        )}
      </div>

      {/* Currently Playing Status Badge */}
      {isCurrentlyPlayingThisTrack && (
        <div
          style={{
            backgroundColor: 'rgba(48, 209, 88, 0.1)',
            border: '1px solid rgba(48, 209, 88, 0.25)',
            borderRadius: '8px',
            padding: '8px 12px',
            fontSize: '12px',
            color: '#30d158',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <PlayCircle size={15} />
            <span>Playing Live Position: <b>{formatTime(currentTime)}</b></span>
          </div>
          <span style={{ fontSize: '11px', color: '#8e8e93' }}>Tap ⏱️ on any line to sync</span>
        </div>
      )}

      {/* Editor Body */}
      {mode === 'lines' ? (
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: '8px',
            maxHeight: '360px',
            overflowY: 'auto',
            paddingRight: '4px',
          }}
        >
          {lines.map((item, idx) => (
            <div
              key={idx}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
                backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                borderRadius: '8px',
                padding: '6px 10px',
              }}
            >
              {/* Timestamp Stamp Button */}
              <button
                onClick={() => stampCurrentTime(idx)}
                disabled={!isCurrentlyPlayingThisTrack}
                title={isCurrentlyPlayingThisTrack ? 'Click to stamp current audio playback time' : 'Play song to stamp'}
                style={{
                  backgroundColor: 'rgba(10, 132, 255, 0.15)',
                  color: 'var(--tg-theme-button-color, #0a84ff)',
                  border: 'none',
                  borderRadius: '6px',
                  padding: '4px 8px',
                  fontSize: '11px',
                  fontWeight: 700,
                  fontFamily: 'monospace',
                  cursor: isCurrentlyPlayingThisTrack ? 'pointer' : 'default',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '4px',
                  flexShrink: 0,
                }}
              >
                <Clock size={12} />
                <span>{formatTime(item.time)}</span>
              </button>

              {/* Lyric Text Input */}
              <input
                type="text"
                value={item.text}
                onChange={(e) => handleLineTextChange(idx, e.target.value)}
                placeholder="Lyric line..."
                style={{
                  flex: 1,
                  background: 'transparent',
                  border: 'none',
                  color: 'var(--tg-theme-text-color, #fff)',
                  fontSize: '13px',
                  outline: 'none',
                }}
              />

              {/* Delete button */}
              <button
                onClick={() => deleteLine(idx)}
                style={{
                  background: 'transparent',
                  border: 'none',
                  color: '#ff453a',
                  cursor: 'pointer',
                  padding: '4px',
                }}
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}

          {/* Add Line button */}
          <button
            onClick={addLine}
            style={{
              padding: '8px',
              borderRadius: '8px',
              border: '1px dashed #48484a',
              backgroundColor: 'transparent',
              color: 'var(--tg-theme-button-color, #0a84ff)',
              fontSize: '13px',
              fontWeight: 600,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '6px',
              cursor: 'pointer',
              marginTop: '4px',
            }}
          >
            <Plus size={16} />
            <span>Add Lyric Line</span>
          </button>
        </div>
      ) : (
        /* Raw LRC textarea */
        <div>
          <textarea
            value={rawText}
            onChange={(e) => setRawText(e.target.value)}
            rows={12}
            placeholder="[00:12.34] Paste timestamped or plain lyrics here..."
            style={{
              width: '100%',
              boxSizing: 'border-box',
              backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
              border: '1px solid #3a3a3c',
              borderRadius: '10px',
              padding: '10px 12px',
              color: 'var(--tg-theme-text-color, #fff)',
              fontSize: '13px',
              fontFamily: 'monospace',
              lineHeight: 1.5,
              outline: 'none',
              resize: 'vertical',
            }}
          />
        </div>
      )}

      {error && (
        <div style={{ color: '#ff453a', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '4px' }}>
          <AlertCircle size={14} />
          <span>{error}</span>
        </div>
      )}

      {/* Review Actions */}
      <div style={{ display: 'flex', gap: '10px', marginTop: '6px' }}>
        <button
          onClick={onCancel}
          disabled={isProposing}
          style={{
            flex: 1,
            padding: '12px',
            borderRadius: '10px',
            border: 'none',
            backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
            color: 'var(--tg-theme-text-color, #fff)',
            fontSize: '14px',
            fontWeight: 600,
            cursor: 'pointer',
          }}
        >
          Cancel
        </button>
        <button
          onClick={handleReviewLyrics}
          disabled={isProposing}
          style={{
            flex: 2,
            padding: '12px',
            borderRadius: '10px',
            border: 'none',
            backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
            color: '#fff',
            fontSize: '14px',
            fontWeight: 700,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '6px',
            cursor: isProposing ? 'not-allowed' : 'pointer',
          }}
        >
          {isProposing ? (
            <>
              <Loader2 size={16} className="animate-spin" />
              <span>Preparing Preview...</span>
            </>
          ) : (
            <>
              <Check size={16} />
              <span>Review Lyrics Change</span>
            </>
          )}
        </button>
      </div>
    </div>
  );
};
