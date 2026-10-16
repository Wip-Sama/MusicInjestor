import React, { useState, useEffect } from 'react';
import {
  X,
  Sliders,
  Search,
  Check,
  Loader2,
  AlertCircle,
  Music,
  User,
  Disc,
  Calendar,
  Hash,
  Link as LinkIcon,
  Sparkles,
} from 'lucide-react';
import { hapticFeedback } from '@telegram-apps/sdk-react';
import { QueueJob, CandidateItem } from '../../types/queue';
import { manualSearch, applyJobEdit } from '../../lib/queueApi';

interface JobEditModalProps {
  isOpen: boolean;
  job: QueueJob | null;
  initialCandidate?: CandidateItem | null;
  onClose: () => void;
  onJobUpdated: (updatedJob: QueueJob) => void;
}

export const JobEditModal: React.FC<JobEditModalProps> = ({
  isOpen,
  job,
  initialCandidate,
  onClose,
  onJobUpdated,
}) => {
  const [title, setTitle] = useState<string>('');
  const [artist, setArtist] = useState<string>('');
  const [album, setAlbum] = useState<string>('');
  const [year, setYear] = useState<string>('');
  const [trackNumber, setTrackNumber] = useState<string>('1');
  const [recordingMbid, setRecordingMbid] = useState<string>('');
  const [releaseMbid, setReleaseMbid] = useState<string>('');

  // Link / Search state
  const [linkQuery, setLinkQuery] = useState<string>('');
  const [isSearching, setIsSearching] = useState<boolean>(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<CandidateItem[]>([]);
  const [successNote, setSuccessNote] = useState<string | null>(null);

  // Submitting state
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    if (!job) return;

    if (initialCandidate) {
      applyCandidate(initialCandidate);
    } else if (job.selected_metadata) {
      setTitle(job.selected_metadata.title || '');
      setArtist(job.selected_metadata.artist || '');
      setAlbum(job.selected_metadata.album || '');
      setYear(job.selected_metadata.year ? job.selected_metadata.year.toString() : '');
      setTrackNumber(job.selected_metadata.track_number ? job.selected_metadata.track_number.toString() : '1');
      setRecordingMbid(job.selected_metadata.recording_mbid || '');
      setReleaseMbid(job.selected_metadata.release_mbid || '');
    } else if (job.candidates && job.candidates.length > 0) {
      applyCandidate(job.candidates[0]);
    } else {
      const stem = job.original_filename.replace(/\.[^/.]+$/, '');
      setTitle(stem || 'Unknown Title');
      setArtist('Unknown Artist');
      setAlbum('Unknown Album');
      setYear('');
      setTrackNumber('1');
      setRecordingMbid('');
      setReleaseMbid('');
    }

    setCandidates(job.candidates || []);
    setSearchError(null);
    setSubmitError(null);
    setSuccessNote(null);
  }, [job, initialCandidate]);

  if (!isOpen || !job) return null;

  const applyCandidate = (cand: CandidateItem) => {
    setTitle(cand.title || '');
    setArtist(cand.artist || '');
    setAlbum(cand.album || '');
    setYear(cand.year ? cand.year.toString() : '');
    setTrackNumber(cand.track_number ? cand.track_number.toString() : '1');
    setRecordingMbid(cand.recording_mbid || '');
    setReleaseMbid(cand.release_mbid || '');
  };

  const handleLinkSearch = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    const query = linkQuery.trim();
    if (!query || isSearching) return;

    setIsSearching(true);
    setSearchError(null);
    setSuccessNote(null);

    try {
      const updatedJob = await manualSearch(job.job_id, query);
      const newCandidates = updatedJob.candidates || [];
      setCandidates(newCandidates);

      if (newCandidates.length === 1) {
        applyCandidate(newCandidates[0]);
        setSuccessNote(`Loaded metadata from match: ${newCandidates[0].artist} - ${newCandidates[0].title}`);
      } else if (newCandidates.length > 1) {
        applyCandidate(newCandidates[0]);
        setSuccessNote(`Found ${newCandidates.length} candidate matches. Top candidate applied to fields below.`);
      } else {
        setSearchError('No matching candidates found on MusicBrainz. Check link or query.');
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Search failed';
      setSearchError(msg);
    } finally {
      setIsSearching(false);
    }
  };

  const handleConfirmApply = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isSubmitting) return;

    setIsSubmitting(true);
    setSubmitError(null);

    const parsedYear = year.trim() ? parseInt(year.trim(), 10) : null;
    const parsedTrack = trackNumber.trim() ? parseInt(trackNumber.trim(), 10) : 1;

    try {
      const updated = await applyJobEdit(job.job_id, {
        title: title.trim(),
        artist: artist.trim(),
        album: album.trim(),
        year: parsedYear && !isNaN(parsedYear) ? parsedYear : null,
        track_number: !isNaN(parsedTrack) ? parsedTrack : 1,
        recording_mbid: recordingMbid || null,
        release_mbid: releaseMbid || null,
      });

      if (hapticFeedback.notificationOccurred.isAvailable()) {
        hapticFeedback.notificationOccurred('success');
      }

      onJobUpdated(updated);
      onClose();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to finalize track ingestion';
      setSubmitError(msg);
      if (hapticFeedback.notificationOccurred.isAvailable()) {
        hapticFeedback.notificationOccurred('error');
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 400,
        backgroundColor: 'rgba(0, 0, 0, 0.75)',
        backdropFilter: 'blur(8px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '16px',
        animation: 'fadeIn 0.2s ease-out',
      }}
    >
      <div
        style={{
          width: '100%',
          maxWidth: '540px',
          maxHeight: '90vh',
          backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
          border: '1px solid var(--tg-theme-secondary-bg-color, #3a3a3c)',
          borderRadius: '16px',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
          boxShadow: '0 12px 32px rgba(0, 0, 0, 0.5)',
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: '14px 18px',
            borderBottom: '1px solid var(--tg-theme-secondary-bg-color, #2c2c2e)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Sliders size={20} color="var(--tg-theme-button-color, #0a84ff)" />
            <div>
              <div style={{ fontSize: '16px', fontWeight: 700, color: 'var(--tg-theme-text-color, #fff)' }}>
                Edit Track Tags &amp; Metadata
              </div>
              <div
                style={{
                  fontSize: '12px',
                  color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                  maxWidth: '320px',
                  whiteSpace: 'nowrap',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                }}
              >
                {job.original_filename}
              </div>
            </div>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            style={{
              background: 'transparent',
              border: 'none',
              color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
              cursor: 'pointer',
              padding: '4px',
            }}
          >
            <X size={20} />
          </button>
        </div>

        {/* Modal Body */}
        <div
          style={{
            padding: '16px 18px',
            overflowY: 'auto',
            display: 'flex',
            flexDirection: 'column',
            gap: '16px',
          }}
        >
          {/* Section 1: Search by Link or Query Card */}
          <div
            style={{
              backgroundColor: 'rgba(10, 132, 255, 0.08)',
              border: '1px solid rgba(10, 132, 255, 0.25)',
              borderRadius: '12px',
              padding: '12px',
              display: 'flex',
              flexDirection: 'column',
              gap: '10px',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', fontWeight: 600 }}>
              <LinkIcon size={16} color="var(--tg-theme-button-color, #0a84ff)" />
              <span>Autofill from MusicBrainz Link or Query</span>
            </div>

            <form
              onSubmit={handleLinkSearch}
              style={{ display: 'flex', gap: '6px' }}
            >
              <label htmlFor="link-query-input" className="visually-hidden" style={{ position: 'absolute', width: 1, height: 1, overflow: 'hidden' }}>
                MusicBrainz URL or search query
              </label>
              <input
                id="link-query-input"
                type="text"
                placeholder="Paste MusicBrainz release/recording URL or Artist - Title..."
                value={linkQuery}
                onChange={(e) => setLinkQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
                    handleLinkSearch(e);
                  }
                }}
                style={{
                  flex: 1,
                  backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
                  border: '1px solid var(--tg-theme-secondary-bg-color, #3a3a3c)',
                  borderRadius: '8px',
                  padding: '8px 10px',
                  color: 'var(--tg-theme-text-color, #fff)',
                  fontSize: '12px',
                  outline: 'none',
                }}
              />
              <button
                type="submit"
                disabled={isSearching || !linkQuery.trim()}
                style={{
                  backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
                  color: '#ffffff',
                  border: 'none',
                  borderRadius: '8px',
                  padding: '8px 14px',
                  fontSize: '12px',
                  fontWeight: 600,
                  cursor: isSearching || !linkQuery.trim() ? 'not-allowed' : 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '4px',
                  whiteSpace: 'nowrap',
                }}
              >
                {isSearching ? <Loader2 size={13} className="spin" /> : <Search size={13} />}
                <span>Fetch Link</span>
              </button>
            </form>

            {searchError && (
              <div style={{ color: '#ff453a', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                <AlertCircle size={13} />
                <span>{searchError}</span>
              </div>
            )}

            {successNote && (
              <div style={{ color: '#30d158', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                <Sparkles size={13} />
                <span>{successNote}</span>
              </div>
            )}

            {/* Candidate selection chips */}
            {candidates.length > 1 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginTop: '4px' }}>
                <div style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', fontWeight: 600 }}>
                  Select candidate to load into fields:
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', maxHeight: '140px', overflowY: 'auto' }}>
                  {candidates.map((cand, idx) => {
                    const pct = Math.round(cand.confidence * 100);
                    return (
                      <button
                        key={cand.index ?? idx}
                        type="button"
                        onClick={() => {
                          applyCandidate(cand);
                          setSuccessNote(`Applied match #${idx + 1}: ${cand.artist} - ${cand.title}`);
                        }}
                        style={{
                          backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
                          border: '1px solid rgba(10, 132, 255, 0.25)',
                          borderRadius: '6px',
                          padding: '6px 8px',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          color: 'var(--tg-theme-text-color, #fff)',
                          cursor: 'pointer',
                          fontSize: '11px',
                          textAlign: 'left',
                        }}
                      >
                        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '380px' }}>
                          <strong>{cand.artist}</strong> - {cand.title} <em>({cand.album})</em>
                        </span>
                        <span style={{ color: pct >= 85 ? '#30d158' : '#ffd60a', fontWeight: 700, marginLeft: '6px' }}>
                          {pct}%
                        </span>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </div>

          {/* Section 2: Editable Metadata Form */}
          <form id="job-edit-form" onSubmit={handleConfirmApply} style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {/* Title */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
              <label htmlFor="field-title" style={{ fontSize: '12px', fontWeight: 600, color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', display: 'flex', alignItems: 'center', gap: '4px' }}>
                <Music size={13} />
                <span>Track Title</span>
              </label>
              <input
                id="field-title"
                type="text"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                required
                style={{
                  backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                  border: '1px solid var(--tg-theme-secondary-bg-color, #3a3a3c)',
                  borderRadius: '8px',
                  padding: '8px 10px',
                  color: 'var(--tg-theme-text-color, #fff)',
                  fontSize: '13px',
                }}
              />
            </div>

            {/* Artist */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
              <label htmlFor="field-artist" style={{ fontSize: '12px', fontWeight: 600, color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', display: 'flex', alignItems: 'center', gap: '4px' }}>
                <User size={13} />
                <span>Artist (Primary Singer / Character)</span>
              </label>
              <input
                id="field-artist"
                type="text"
                value={artist}
                onChange={(e) => setArtist(e.target.value)}
                required
                style={{
                  backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                  border: '1px solid var(--tg-theme-secondary-bg-color, #3a3a3c)',
                  borderRadius: '8px',
                  padding: '8px 10px',
                  color: 'var(--tg-theme-text-color, #fff)',
                  fontSize: '13px',
                }}
              />
            </div>

            {/* Album */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
              <label htmlFor="field-album" style={{ fontSize: '12px', fontWeight: 600, color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', display: 'flex', alignItems: 'center', gap: '4px' }}>
                <Disc size={13} />
                <span>Album / Release Title</span>
              </label>
              <input
                id="field-album"
                type="text"
                value={album}
                onChange={(e) => setAlbum(e.target.value)}
                required
                style={{
                  backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                  border: '1px solid var(--tg-theme-secondary-bg-color, #3a3a3c)',
                  borderRadius: '8px',
                  padding: '8px 10px',
                  color: 'var(--tg-theme-text-color, #fff)',
                  fontSize: '13px',
                }}
              />
            </div>

            {/* Year & Track Number Row */}
            <div style={{ display: 'flex', gap: '10px' }}>
              <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: '4px' }}>
                <label htmlFor="field-year" style={{ fontSize: '12px', fontWeight: 600, color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', display: 'flex', alignItems: 'center', gap: '4px' }}>
                  <Calendar size={13} />
                  <span>Release Year</span>
                </label>
                <input
                  id="field-year"
                  type="number"
                  placeholder="YYYY"
                  value={year}
                  onChange={(e) => setYear(e.target.value)}
                  style={{
                    backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                    border: '1px solid var(--tg-theme-secondary-bg-color, #3a3a3c)',
                    borderRadius: '8px',
                    padding: '8px 10px',
                    color: 'var(--tg-theme-text-color, #fff)',
                    fontSize: '13px',
                  }}
                />
              </div>

              <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: '4px' }}>
                <label htmlFor="field-track" style={{ fontSize: '12px', fontWeight: 600, color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', display: 'flex', alignItems: 'center', gap: '4px' }}>
                  <Hash size={13} />
                  <span>Track Number</span>
                </label>
                <input
                  id="field-track"
                  type="number"
                  min="1"
                  value={trackNumber}
                  onChange={(e) => setTrackNumber(e.target.value)}
                  style={{
                    backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                    border: '1px solid var(--tg-theme-secondary-bg-color, #3a3a3c)',
                    borderRadius: '8px',
                    padding: '8px 10px',
                    color: 'var(--tg-theme-text-color, #fff)',
                    fontSize: '13px',
                  }}
                />
              </div>
            </div>

            {submitError && (
              <div style={{ color: '#ff453a', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                <AlertCircle size={14} />
                <span>{submitError}</span>
              </div>
            )}
          </form>
        </div>

        {/* Modal Footer */}
        <div
          style={{
            padding: '14px 18px',
            borderTop: '1px solid var(--tg-theme-secondary-bg-color, #2c2c2e)',
            display: 'flex',
            justifyContent: 'flex-end',
            gap: '8px',
          }}
        >
          <button
            type="button"
            onClick={onClose}
            disabled={isSubmitting}
            style={{
              backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
              color: 'var(--tg-theme-text-color, #fff)',
              border: 'none',
              borderRadius: '8px',
              padding: '8px 14px',
              fontSize: '13px',
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            Cancel
          </button>
          <button
            type="submit"
            form="job-edit-form"
            disabled={isSubmitting || !title.trim() || !artist.trim()}
            style={{
              backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
              color: '#ffffff',
              border: 'none',
              borderRadius: '8px',
              padding: '8px 16px',
              fontSize: '13px',
              fontWeight: 600,
              cursor: isSubmitting || !title.trim() || !artist.trim() ? 'not-allowed' : 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            {isSubmitting ? <Loader2 size={14} className="spin" /> : <Check size={14} />}
            <span>Confirm &amp; Ingest Track</span>
          </button>
        </div>
      </div>
    </div>
  );
};

