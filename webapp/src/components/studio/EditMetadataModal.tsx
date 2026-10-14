import React, { useState } from 'react';
import {
  Sparkles,
  Search,
  Check,
  Loader2,
  AlertCircle,
  Hash,
  Calendar,
  Disc,
  User,
  Music,
} from 'lucide-react';
import { apiFetch } from '../../lib/auth';
import { TrackItem } from '../../types/music';
import { AutoEnrichCandidate, AutoEnrichResponse, ProposalResponse } from '../../types/studio';

interface EditMetadataModalProps {
  track: TrackItem;
  onPropose: (proposal: ProposalResponse) => void;
  isProposing: boolean;
  onCancel: () => void;
}

export const EditMetadataModal: React.FC<EditMetadataModalProps> = ({
  track,
  onPropose,
  isProposing,
  onCancel,
}) => {
  const [title, setTitle] = useState<string>(track.title);
  const [artist, setArtist] = useState<string>(track.artist);
  const [album, setAlbum] = useState<string>(track.album);
  const [year, setYear] = useState<string>(track.year ? track.year.toString() : '');
  const [trackNumber, setTrackNumber] = useState<string>(
    track.track_number ? track.track_number.toString() : '1'
  );

  // Auto-enrich states
  const [isSearching, setIsSearching] = useState<boolean>(false);
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [candidates, setCandidates] = useState<AutoEnrichCandidate[]>([]);
  const [selectedMbid, setSelectedMbid] = useState<{ rec?: string; rel?: string }>({});
  const [enrichError, setEnrichError] = useState<string | null>(null);
  const [fetchedLyrics, setFetchedLyrics] = useState<string | null>(null);

  const handleAutoSearch = async (customQuery?: string) => {
    setIsSearching(true);
    setEnrichError(null);
    try {
      const q = customQuery !== undefined ? customQuery : searchQuery;
      const res = await apiFetch(`/api/v1/app/tracks/${track.id}/auto-enrich`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: q }),
      });
      if (!res.ok) {
        throw new Error(`Enrichment failed (${res.status})`);
      }
      const data: AutoEnrichResponse = await res.json();
      setCandidates(data.candidates || []);
      setFetchedLyrics(data.fetched_lyrics || null);
      if (!data.candidates || data.candidates.length === 0) {
        setEnrichError('No MusicBrainz matches found. Try entering a custom query.');
      }
    } catch (err: unknown) {
      setEnrichError((err as Error).message || 'Failed to query MusicBrainz');
    } finally {
      setIsSearching(false);
    }
  };

  const applyCandidate = (cand: AutoEnrichCandidate) => {
    setTitle(cand.title);
    setArtist(cand.artist);
    setAlbum(cand.album);
    if (cand.year) setYear(cand.year.toString());
    if (cand.track_number) setTrackNumber(cand.track_number.toString());
    setSelectedMbid({ rec: cand.recording_mbid, rel: cand.release_mbid });
    setCandidates([]);
  };

  const handleReviewClick = async () => {
    const yr = year.trim() ? parseInt(year.trim(), 10) : null;
    const trk = trackNumber.trim() ? parseInt(trackNumber.trim(), 10) : 1;

    const body: Record<string, unknown> = {
      action_type: 'metadata',
      title: title.trim(),
      artist: artist.trim(),
      album: album.trim(),
      year: isNaN(yr ?? NaN) ? null : yr,
      track_number: isNaN(trk) ? 1 : trk,
      recording_mbid: selectedMbid.rec || null,
      release_mbid: selectedMbid.rel || null,
    };
    if (fetchedLyrics) {
      body.lyrics = fetchedLyrics;
    }

    const res = await apiFetch(`/api/v1/app/tracks/${track.id}/propose-edit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.error || `Proposal failed (${res.status})`);
    }

    const proposal: ProposalResponse = await res.json();
    onPropose(proposal);
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      {/* Auto Re-Search Toolbar */}
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
            <span>Auto-Enrich with MusicBrainz &amp; Genius</span>
          </div>
          <button
            onClick={() => handleAutoSearch()}
            disabled={isSearching}
            style={{
              backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
              color: '#fff',
              border: 'none',
              borderRadius: '8px',
              padding: '6px 12px',
              fontSize: '12px',
              fontWeight: 600,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
            }}
          >
            {isSearching ? <Loader2 size={13} className="animate-spin" /> : <Search size={13} />}
            <span>Auto Search</span>
          </button>
        </div>

        {/* Custom Query Search row */}
        <div style={{ display: 'flex', gap: '6px' }}>
          <input
            type="text"
            placeholder="Custom query: Artist - Title | Album or URL..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleAutoSearch(searchQuery);
            }}
            style={{
              flex: 1,
              backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
              border: '1px solid var(--tg-theme-secondary-bg-color, #3a3a3c)',
              borderRadius: '8px',
              padding: '6px 10px',
              color: 'var(--tg-theme-text-color, #fff)',
              fontSize: '12px',
              outline: 'none',
            }}
          />
          <button
            onClick={() => handleAutoSearch(searchQuery)}
            disabled={isSearching || !searchQuery.trim()}
            style={{
              backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
              color: 'var(--tg-theme-text-color, #fff)',
              border: 'none',
              borderRadius: '8px',
              padding: '6px 10px',
              fontSize: '12px',
              cursor: 'pointer',
            }}
          >
            Search
          </button>
        </div>

        {enrichError && (
          <div style={{ color: '#ff453a', fontSize: '11px', display: 'flex', alignItems: 'center', gap: '4px' }}>
            <AlertCircle size={12} />
            <span>{enrichError}</span>
          </div>
        )}

        {/* Candidates List dropdown/picker */}
        {candidates.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginTop: '4px' }}>
            <div style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', fontWeight: 600 }}>
              Select a candidate match to autofill:
            </div>
            {candidates.map((c, i) => (
              <div
                key={i}
                onClick={() => applyCandidate(c)}
                style={{
                  backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
                  border: '1px solid rgba(10, 132, 255, 0.3)',
                  borderRadius: '8px',
                  padding: '8px 10px',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  cursor: 'pointer',
                  fontSize: '12px',
                }}
              >
                <div>
                  <b>{c.title}</b> — <span style={{ color: '#8e8e93' }}>{c.artist}</span> ({c.album})
                </div>
                <span
                  style={{
                    backgroundColor: 'rgba(48, 209, 88, 0.15)',
                    color: '#30d158',
                    padding: '2px 6px',
                    borderRadius: '4px',
                    fontWeight: 700,
                    fontSize: '11px',
                  }}
                >
                  {c.confidence_pct}%
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Manual Input Fields */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        {/* Title */}
        <div>
          <label style={{ fontSize: '12px', fontWeight: 600, color: '#8e8e93', display: 'flex', alignItems: 'center', gap: '4px', marginBottom: '4px' }}>
            <Music size={13} /> Track Title
          </label>
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            style={{
              width: '100%',
              boxSizing: 'border-box',
              backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
              border: '1px solid #3a3a3c',
              borderRadius: '8px',
              padding: '10px 12px',
              color: 'var(--tg-theme-text-color, #fff)',
              fontSize: '14px',
              outline: 'none',
            }}
          />
        </div>

        {/* Artist */}
        <div>
          <label style={{ fontSize: '12px', fontWeight: 600, color: '#8e8e93', display: 'flex', alignItems: 'center', gap: '4px', marginBottom: '4px' }}>
            <User size={13} /> Artist
          </label>
          <input
            type="text"
            value={artist}
            onChange={(e) => setArtist(e.target.value)}
            style={{
              width: '100%',
              boxSizing: 'border-box',
              backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
              border: '1px solid #3a3a3c',
              borderRadius: '8px',
              padding: '10px 12px',
              color: 'var(--tg-theme-text-color, #fff)',
              fontSize: '14px',
              outline: 'none',
            }}
          />
        </div>

        {/* Album */}
        <div>
          <label style={{ fontSize: '12px', fontWeight: 600, color: '#8e8e93', display: 'flex', alignItems: 'center', gap: '4px', marginBottom: '4px' }}>
            <Disc size={13} /> Album
          </label>
          <input
            type="text"
            value={album}
            onChange={(e) => setAlbum(e.target.value)}
            style={{
              width: '100%',
              boxSizing: 'border-box',
              backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
              border: '1px solid #3a3a3c',
              borderRadius: '8px',
              padding: '10px 12px',
              color: 'var(--tg-theme-text-color, #fff)',
              fontSize: '14px',
              outline: 'none',
            }}
          />
        </div>

        {/* Year and Track Number row */}
        <div style={{ display: 'flex', gap: '10px' }}>
          <div style={{ flex: 1 }}>
            <label style={{ fontSize: '12px', fontWeight: 600, color: '#8e8e93', display: 'flex', alignItems: 'center', gap: '4px', marginBottom: '4px' }}>
              <Calendar size={13} /> Year
            </label>
            <input
              type="number"
              value={year}
              onChange={(e) => setYear(e.target.value)}
              placeholder="e.g. 2024"
              style={{
                width: '100%',
                boxSizing: 'border-box',
                backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                border: '1px solid #3a3a3c',
                borderRadius: '8px',
                padding: '10px 12px',
                color: 'var(--tg-theme-text-color, #fff)',
                fontSize: '14px',
                outline: 'none',
              }}
            />
          </div>

          <div style={{ flex: 1 }}>
            <label style={{ fontSize: '12px', fontWeight: 600, color: '#8e8e93', display: 'flex', alignItems: 'center', gap: '4px', marginBottom: '4px' }}>
              <Hash size={13} /> Track #
            </label>
            <input
              type="number"
              value={trackNumber}
              onChange={(e) => setTrackNumber(e.target.value)}
              placeholder="e.g. 1"
              style={{
                width: '100%',
                boxSizing: 'border-box',
                backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                border: '1px solid #3a3a3c',
                borderRadius: '8px',
                padding: '10px 12px',
                color: 'var(--tg-theme-text-color, #fff)',
                fontSize: '14px',
                outline: 'none',
              }}
            />
          </div>
        </div>
      </div>

      {/* Review Diff Button */}
      <div style={{ display: 'flex', gap: '10px', marginTop: '8px' }}>
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
          onClick={handleReviewClick}
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
          {isProposing ? <Loader2 size={16} className="animate-spin" /> : <Check size={16} />}
          <span>Review Changes (Dry-Run)</span>
        </button>
      </div>
    </div>
  );
};

