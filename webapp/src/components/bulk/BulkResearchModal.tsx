import React, { useState } from 'react';
import {
  X,
  Sparkles,
  Search,
  Music,
  CheckCircle2,
  AlertCircle,
  Clock,
  ArrowRight,
  ChevronLeft,
  ChevronRight,
  Check,
  SkipForward,
  Trash2,
  ListChecks,
} from 'lucide-react';
import { apiFetch } from '../../lib/auth';

interface BulkResearchModalProps {
  isOpen: boolean;
  onClose: () => void;
  selectedTrackIds?: string[];
  searchQuery?: string;
  totalCatalogTracks?: number;
  onBulkCompleted: () => void;
}

type BulkScope = 'selected' | 'search' | 'missing_lyrics' | 'missing_mbid' | 'all';
type ModalStep = 'scope' | 'executing' | 'review' | 'done';

interface BulkPlan {
  total_tracks: number;
  missing_lyrics_count: number;
  missing_mbid_count: number;
  estimated_seconds: number;
  track_ids: string[];
}

interface ReviewItem {
  track_id: string;
  title: string;
  status: string;
  proposed_meta?: {
    title: string;
    artist: string;
    album: string;
    year: number | null;
    track_number: number | null;
    recording_mbid?: string | null;
    release_mbid?: string | null;
  };
  proposed_lyrics?: string | null;
}

interface BulkReport {
  total_tracks: number;
  applied_count: number;
  needs_review_count: number;
  skipped_count: number;
  failed_count: number;
  elapsed_seconds: number;
  details: ReviewItem[];
}

export const BulkResearchModal: React.FC<BulkResearchModalProps> = ({
  isOpen,
  onClose,
  selectedTrackIds = [],
  searchQuery = '',
  onBulkCompleted,
}) => {
  const hasSelected = selectedTrackIds.length > 0;
  const hasFilter = searchQuery.trim().length > 0;

  const defaultScope: BulkScope = hasSelected ? 'selected' : hasFilter ? 'search' : 'all';

  const [step, setStep] = useState<ModalStep>('scope');
  const [scope, setScope] = useState<BulkScope>(defaultScope);
  const [searchMetadata, setSearchMetadata] = useState<boolean>(true);
  const [searchLyrics, setSearchLyrics] = useState<boolean>(true);
  const [autoApply, setAutoApply] = useState<boolean>(true);

  const [plan, setPlan] = useState<BulkPlan | null>(null);
  const [isPlanning, setIsPlanning] = useState<boolean>(false);
  const [report, setReport] = useState<BulkReport | null>(null);

  const [reviewItems, setReviewItems] = useState<ReviewItem[]>([]);
  const [reviewIndex, setReviewIndex] = useState<number>(0);
  const [isApplyingTrack, setIsApplyingTrack] = useState<boolean>(false);

  if (!isOpen) return null;

  const resolveTargetTrackIds = (): string[] | undefined => {
    if (scope === 'selected' && hasSelected) {
      return selectedTrackIds;
    }
    return undefined;
  };

  const resolveFilterMode = (): string => {
    if (scope === 'selected') return 'all';
    if (scope === 'search') return 'all';
    return scope;
  };

  const handlePlanDryRun = async () => {
    setIsPlanning(true);
    try {
      const res = await apiFetch('/api/v1/app/tracks/bulk-research/plan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          track_ids: resolveTargetTrackIds(),
          filter_mode: resolveFilterMode(),
          search_metadata: searchMetadata,
          search_lyrics: searchLyrics,
          auto_apply: autoApply,
        }),
      });
      if (res.ok) {
        const data: BulkPlan = await res.json();
        setPlan(data);
      }
    } catch (err) {
      console.warn('Failed to plan bulk research:', err);
    } finally {
      setIsPlanning(false);
    }
  };

  const handleExecute = async () => {
    setStep('executing');
    try {
      const res = await apiFetch('/api/v1/app/tracks/bulk-research/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          track_ids: resolveTargetTrackIds(),
          filter_mode: resolveFilterMode(),
          search_metadata: searchMetadata,
          search_lyrics: searchLyrics,
          auto_apply: autoApply,
        }),
      });

      if (!res.ok) {
        throw new Error('Bulk execution failed');
      }

      const rep: BulkReport = await res.json();
      setReport(rep);

      const needsRev = (rep.details || []).filter((d) => d.status === 'needs_review');
      if (needsRev.length > 0) {
        setReviewItems(needsRev);
        setReviewIndex(0);
        setStep('review');
      } else {
        setStep('done');
      }
      onBulkCompleted();
    } catch (err) {
      console.error('Execution error in bulk research:', err);
      setStep('done');
    }
  };

  const handleApplySingleReview = async () => {
    if (reviewItems.length === 0) return;
    const item = reviewItems[reviewIndex];
    if (!item.proposed_meta) return;

    setIsApplyingTrack(true);
    try {
      await apiFetch(`/api/v1/app/tracks/${item.track_id}/apply-edit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action_type: 'bulk_review',
          new_title: item.proposed_meta.title,
          new_artist: item.proposed_meta.artist,
          new_album: item.proposed_meta.album,
          new_year: item.proposed_meta.year,
          new_track_number: item.proposed_meta.track_number,
          new_lyrics: item.proposed_lyrics || null,
        }),
      });

      const nextItems = reviewItems.filter((_, idx) => idx !== reviewIndex);
      setReviewItems(nextItems);
      if (nextItems.length === 0) {
        setStep('done');
      } else {
        setReviewIndex(Math.min(reviewIndex, nextItems.length - 1));
      }
      onBulkCompleted();
    } catch (err) {
      console.warn('Failed to apply review track:', err);
    } finally {
      setIsApplyingTrack(false);
    }
  };

  const handleDiscardSingleReview = () => {
    const nextItems = reviewItems.filter((_, idx) => idx !== reviewIndex);
    setReviewItems(nextItems);
    if (nextItems.length === 0) {
      setStep('done');
    } else {
      setReviewIndex(Math.min(reviewIndex, nextItems.length - 1));
    }
  };

  const handleApplyAllReview = async () => {
    setIsApplyingTrack(true);
    try {
      for (const item of reviewItems) {
        if (!item.proposed_meta) continue;
        await apiFetch(`/api/v1/app/tracks/${item.track_id}/apply-edit`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            action_type: 'bulk_review',
            new_title: item.proposed_meta.title,
            new_artist: item.proposed_meta.artist,
            new_album: item.proposed_meta.album,
            new_year: item.proposed_meta.year,
            new_track_number: item.proposed_meta.track_number,
            new_lyrics: item.proposed_lyrics || null,
          }),
        });
      }
      setReviewItems([]);
      setStep('done');
      onBulkCompleted();
    } catch (err) {
      console.warn('Failed to apply all pending items:', err);
    } finally {
      setIsApplyingTrack(false);
    }
  };

  const handleDiscardAllReview = () => {
    setReviewItems([]);
    setStep('done');
  };

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        backgroundColor: 'rgba(0, 0, 0, 0.75)',
        zIndex: 1000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '16px',
        backdropFilter: 'blur(6px)',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: '100%',
          maxWidth: '480px',
          maxHeight: '90vh',
          backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
          borderRadius: '20px',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
          boxShadow: '0 20px 50px rgba(0, 0, 0, 0.6)',
          border: '1px solid rgba(255, 255, 255, 0.1)',
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: '16px 20px',
            borderBottom: '1px solid rgba(255, 255, 255, 0.08)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Sparkles size={20} color="var(--tg-theme-button-color, #0a84ff)" />
            <span style={{ fontSize: '17px', fontWeight: 700, color: 'var(--tg-theme-text-color, #fff)' }}>
              {step === 'review'
                ? `Review Matches (${reviewIndex + 1}/${reviewItems.length})`
                : 'Bulk Metadata & Lyrics Re-Search'}
            </span>
          </div>
          <button
            onClick={onClose}
            style={{
              background: 'rgba(255, 255, 255, 0.1)',
              border: 'none',
              borderRadius: '50%',
              width: '32px',
              height: '32px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'var(--tg-theme-text-color, #fff)',
              cursor: 'pointer',
            }}
          >
            <X size={18} />
          </button>
        </div>

        {/* Scrollable Body */}
        <div
          style={{
            padding: '20px',
            overflowY: 'auto',
            display: 'flex',
            flexDirection: 'column',
            gap: '16px',
          }}
        >
          {/* STEP 1: Scope & Planning */}
          {step === 'scope' && (
            <>
              {/* Scope Selection */}
              <div>
                <div
                  style={{
                    fontSize: '13px',
                    fontWeight: 700,
                    textTransform: 'uppercase',
                    color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                    letterSpacing: '0.5px',
                    marginBottom: '8px',
                  }}
                >
                  1. Choose Target Scope
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  {hasSelected && (
                    <button
                      onClick={() => {
                        setScope('selected');
                        setPlan(null);
                      }}
                      style={{
                        padding: '12px 14px',
                        borderRadius: '12px',
                        border: scope === 'selected'
                          ? '1.5px solid var(--tg-theme-button-color, #0a84ff)'
                          : '1px solid rgba(255, 255, 255, 0.08)',
                        backgroundColor: scope === 'selected'
                          ? 'rgba(10, 132, 255, 0.15)'
                          : 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                        color: 'var(--tg-theme-text-color, #fff)',
                        cursor: 'pointer',
                        textAlign: 'left',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                        <ListChecks size={18} color="var(--tg-theme-button-color, #0a84ff)" />
                        <div>
                          <div style={{ fontSize: '14px', fontWeight: 700 }}>
                            Selected Tracks ({selectedTrackIds.length})
                          </div>
                          <div style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                            Run bulk operations only on manually checked songs.
                          </div>
                        </div>
                      </div>
                      {scope === 'selected' && <CheckCircle2 size={18} color="var(--tg-theme-button-color, #0a84ff)" />}
                    </button>
                  )}

                  {hasFilter && (
                    <button
                      onClick={() => {
                        setScope('search');
                        setPlan(null);
                      }}
                      style={{
                        padding: '12px 14px',
                        borderRadius: '12px',
                        border: scope === 'search'
                          ? '1.5px solid var(--tg-theme-button-color, #0a84ff)'
                          : '1px solid rgba(255, 255, 255, 0.08)',
                        backgroundColor: scope === 'search'
                          ? 'rgba(10, 132, 255, 0.15)'
                          : 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                        color: 'var(--tg-theme-text-color, #fff)',
                        cursor: 'pointer',
                        textAlign: 'left',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                        <Search size={18} color="#ff9f0a" />
                        <div>
                          <div style={{ fontSize: '14px', fontWeight: 700 }}>
                            Current Search Query ("{searchQuery.slice(0, 20)}...")
                          </div>
                          <div style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                            Target only songs matching your active search filter.
                          </div>
                        </div>
                      </div>
                      {scope === 'search' && <CheckCircle2 size={18} color="var(--tg-theme-button-color, #0a84ff)" />}
                    </button>
                  )}

                  <button
                    onClick={() => {
                      setScope('missing_lyrics');
                      setPlan(null);
                    }}
                    style={{
                      padding: '12px 14px',
                      borderRadius: '12px',
                      border: scope === 'missing_lyrics'
                        ? '1.5px solid var(--tg-theme-button-color, #0a84ff)'
                        : '1px solid rgba(255, 255, 255, 0.08)',
                      backgroundColor: scope === 'missing_lyrics'
                        ? 'rgba(10, 132, 255, 0.15)'
                        : 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                      color: 'var(--tg-theme-text-color, #fff)',
                      cursor: 'pointer',
                      textAlign: 'left',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                      <Music size={18} color="#30d158" />
                      <div>
                        <div style={{ fontSize: '14px', fontWeight: 700 }}>Tracks Missing Synced Lyrics</div>
                        <div style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                          Find and embed LRCLIB synced lyrics for tracks lacking them.
                        </div>
                      </div>
                    </div>
                    {scope === 'missing_lyrics' && <CheckCircle2 size={18} color="var(--tg-theme-button-color, #0a84ff)" />}
                  </button>

                  <button
                    onClick={() => {
                      setScope('missing_mbid');
                      setPlan(null);
                    }}
                    style={{
                      padding: '12px 14px',
                      borderRadius: '12px',
                      border: scope === 'missing_mbid'
                        ? '1.5px solid var(--tg-theme-button-color, #0a84ff)'
                        : '1px solid rgba(255, 255, 255, 0.08)',
                      backgroundColor: scope === 'missing_mbid'
                        ? 'rgba(10, 132, 255, 0.15)'
                        : 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                      color: 'var(--tg-theme-text-color, #fff)',
                      cursor: 'pointer',
                      textAlign: 'left',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                      <AlertCircle size={18} color="#bf5af2" />
                      <div>
                        <div style={{ fontSize: '14px', fontWeight: 700 }}>Tracks Missing Release Date / Tags</div>
                        <div style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                          Re-query MusicBrainz for incomplete metadata records.
                        </div>
                      </div>
                    </div>
                    {scope === 'missing_mbid' && <CheckCircle2 size={18} color="var(--tg-theme-button-color, #0a84ff)" />}
                  </button>

                  <button
                    onClick={() => {
                      setScope('all');
                      setPlan(null);
                    }}
                    style={{
                      padding: '12px 14px',
                      borderRadius: '12px',
                      border: scope === 'all'
                        ? '1.5px solid var(--tg-theme-button-color, #0a84ff)'
                        : '1px solid rgba(255, 255, 255, 0.08)',
                      backgroundColor: scope === 'all'
                        ? 'rgba(10, 132, 255, 0.15)'
                        : 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                      color: 'var(--tg-theme-text-color, #fff)',
                      cursor: 'pointer',
                      textAlign: 'left',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                      <Sparkles size={18} color="var(--tg-theme-button-color, #0a84ff)" />
                      <div>
                        <div style={{ fontSize: '14px', fontWeight: 700 }}>Entire Music Library</div>
                        <div style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                          Run polite throttled re-search across all catalog audio files.
                        </div>
                      </div>
                    </div>
                    {scope === 'all' && <CheckCircle2 size={18} color="var(--tg-theme-button-color, #0a84ff)" />}
                  </button>
                </div>
              </div>

              {/* Toggles */}
              <div>
                <div
                  style={{
                    fontSize: '13px',
                    fontWeight: 700,
                    textTransform: 'uppercase',
                    color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                    letterSpacing: '0.5px',
                    marginBottom: '8px',
                  }}
                >
                  2. Operations &amp; Safety
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  <label
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      padding: '10px 14px',
                      backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                      borderRadius: '10px',
                      cursor: 'pointer',
                    }}
                  >
                    <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--tg-theme-text-color, #fff)' }}>
                      Search Online Metadata (MusicBrainz, Genius, Last.fm)
                    </span>
                    <input
                      type="checkbox"
                      checked={searchMetadata}
                      onChange={(e) => setSearchMetadata(e.target.checked)}
                      style={{ accentColor: 'var(--tg-theme-button-color, #0a84ff)' }}
                    />
                  </label>

                  <label
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      padding: '10px 14px',
                      backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                      borderRadius: '10px',
                      cursor: 'pointer',
                    }}
                  >
                    <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--tg-theme-text-color, #fff)' }}>
                      Search Synced Lyrics (LRCLIB)
                    </span>
                    <input
                      type="checkbox"
                      checked={searchLyrics}
                      onChange={(e) => setSearchLyrics(e.target.checked)}
                      style={{ accentColor: 'var(--tg-theme-button-color, #0a84ff)' }}
                    />
                  </label>

                  <label
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      padding: '10px 14px',
                      backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                      borderRadius: '10px',
                      cursor: 'pointer',
                    }}
                  >
                    <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--tg-theme-text-color, #fff)' }}>
                      Auto-apply high confidence matches (≥ 98%)
                    </span>
                    <input
                      type="checkbox"
                      checked={autoApply}
                      onChange={(e) => setAutoApply(e.target.checked)}
                      style={{ accentColor: 'var(--tg-theme-button-color, #0a84ff)' }}
                    />
                  </label>
                </div>
              </div>

              {/* Dry-Run Plan Summary (if planned) */}
              {plan && (
                <div
                  style={{
                    padding: '14px',
                    borderRadius: '12px',
                    backgroundColor: 'rgba(10, 132, 255, 0.1)',
                    border: '1px solid rgba(10, 132, 255, 0.3)',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '6px',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <Clock size={16} color="var(--tg-theme-button-color, #0a84ff)" />
                    <span style={{ fontSize: '13px', fontWeight: 700, color: 'var(--tg-theme-button-color, #0a84ff)' }}>
                      Plan Preview: {plan.total_tracks} tracks selected
                    </span>
                  </div>
                  <div style={{ fontSize: '12px', color: 'var(--tg-theme-text-color, #fff)' }}>
                    • Missing Lyrics: <b>{plan.missing_lyrics_count}</b> | Missing MBID: <b>{plan.missing_mbid_count}</b>
                  </div>
                  <div style={{ fontSize: '12px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                    Estimated time: ~{Math.floor(plan.estimated_seconds / 60)}m {plan.estimated_seconds % 60}s (at polite throttled rate limits)
                  </div>
                </div>
              )}
            </>
          )}

          {/* STEP 2: Executing */}
          {step === 'executing' && (
            <div
              style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                padding: '40px 20px',
                textAlign: 'center',
                gap: '16px',
              }}
            >
              <div
                style={{
                  width: '56px',
                  height: '56px',
                  borderRadius: '50%',
                  border: '3px solid rgba(10, 132, 255, 0.2)',
                  borderTopColor: 'var(--tg-theme-button-color, #0a84ff)',
                  animation: 'spin 1s linear infinite',
                }}
              />
              <div>
                <div style={{ fontSize: '17px', fontWeight: 700, color: 'var(--tg-theme-text-color, #fff)' }}>
                  Executing Bulk Re-Search
                </div>
                <div style={{ fontSize: '13px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', marginTop: '6px' }}>
                  Querying MusicBrainz (1 req/s) and LRCLIB (3 req/s). High confidence matches will be applied automatically.
                </div>
              </div>
            </div>
          )}

          {/* STEP 3: Interactive Review for Ambiguous Matches */}
          {step === 'review' && reviewItems.length > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
              <div
                style={{
                  padding: '10px 14px',
                  borderRadius: '10px',
                  backgroundColor: 'rgba(255, 159, 10, 0.15)',
                  border: '1px solid rgba(255, 159, 10, 0.3)',
                  color: '#ff9f0a',
                  fontSize: '12px',
                  fontWeight: 600,
                  display: 'flex',
                  alignItems: 'center',
                  gap: '8px',
                }}
              >
                <AlertCircle size={16} />
                <span>
                  {reviewItems.length} track(s) have online matches requiring your review before applying.
                </span>
              </div>

              {/* Single Track Diff Card */}
              {(() => {
                const item = reviewItems[reviewIndex];
                const pm = item.proposed_meta || {
                  title: item.title,
                  artist: '',
                  album: '',
                  year: null,
                  track_number: null,
                };
                return (
                  <div
                    style={{
                      padding: '16px',
                      borderRadius: '14px',
                      backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                      border: '1px solid rgba(255, 255, 255, 0.08)',
                      display: 'flex',
                      flexDirection: 'column',
                      gap: '12px',
                    }}
                  >
                    <div>
                      <span style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', fontWeight: 700, textTransform: 'uppercase' }}>
                        Proposed Metadata Changes
                      </span>
                      <div style={{ fontSize: '16px', fontWeight: 700, color: 'var(--tg-theme-text-color, #fff)', marginTop: '4px' }}>
                        {pm.artist} — {pm.title}
                      </div>
                    </div>

                    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', fontSize: '13px' }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid rgba(255, 255, 255, 0.05)' }}>
                        <span style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>Album:</span>
                        <span style={{ fontWeight: 600, color: 'var(--tg-theme-text-color, #fff)' }}>{pm.album}</span>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid rgba(255, 255, 255, 0.05)' }}>
                        <span style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>Release Year:</span>
                        <span style={{ fontWeight: 600, color: pm.year ? '#30d158' : '#8e8e93' }}>
                          {pm.year || 'Preserved'}
                        </span>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0' }}>
                        <span style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>Synced Lyrics:</span>
                        <span style={{ fontWeight: 600, color: item.proposed_lyrics ? '#30d158' : '#8e8e93' }}>
                          {item.proposed_lyrics ? 'Found Online (Synced)' : 'None'}
                        </span>
                      </div>
                    </div>

                    {/* Single Track Decision Controls */}
                    <div style={{ display: 'flex', gap: '8px', marginTop: '6px' }}>
                      <button
                        disabled={isApplyingTrack}
                        onClick={handleApplySingleReview}
                        style={{
                          flex: 1,
                          padding: '10px',
                          borderRadius: '8px',
                          border: 'none',
                          backgroundColor: '#30d158',
                          color: '#fff',
                          fontWeight: 700,
                          fontSize: '13px',
                          cursor: 'pointer',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          gap: '6px',
                        }}
                      >
                        <Check size={16} />
                        <span>Accept</span>
                      </button>

                      <button
                        disabled={isApplyingTrack}
                        onClick={() => setReviewIndex((idx) => (idx + 1) % reviewItems.length)}
                        style={{
                          padding: '10px 14px',
                          borderRadius: '8px',
                          border: '1px solid rgba(255, 255, 255, 0.1)',
                          backgroundColor: 'rgba(255, 255, 255, 0.05)',
                          color: 'var(--tg-theme-text-color, #fff)',
                          fontWeight: 600,
                          fontSize: '13px',
                          cursor: 'pointer',
                          display: 'flex',
                          alignItems: 'center',
                          gap: '4px',
                        }}
                      >
                        <SkipForward size={14} />
                        <span>Skip</span>
                      </button>

                      <button
                        disabled={isApplyingTrack}
                        onClick={handleDiscardSingleReview}
                        style={{
                          padding: '10px 14px',
                          borderRadius: '8px',
                          border: 'none',
                          backgroundColor: 'rgba(255, 69, 58, 0.15)',
                          color: '#ff453a',
                          fontWeight: 600,
                          fontSize: '13px',
                          cursor: 'pointer',
                          display: 'flex',
                          alignItems: 'center',
                          gap: '4px',
                        }}
                      >
                        <Trash2 size={14} />
                        <span>Discard</span>
                      </button>
                    </div>

                    {/* Pagination between review items */}
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        paddingTop: '6px',
                        borderTop: '1px solid rgba(255, 255, 255, 0.05)',
                      }}
                    >
                      <button
                        disabled={reviewIndex <= 0}
                        onClick={() => setReviewIndex((idx) => Math.max(0, idx - 1))}
                        style={{
                          background: 'transparent',
                          border: 'none',
                          color: reviewIndex <= 0 ? '#555' : 'var(--tg-theme-button-color, #0a84ff)',
                          cursor: reviewIndex <= 0 ? 'not-allowed' : 'pointer',
                          display: 'flex',
                          alignItems: 'center',
                          fontSize: '12px',
                          fontWeight: 600,
                        }}
                      >
                        <ChevronLeft size={16} /> Prev
                      </button>

                      <span style={{ fontSize: '12px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                        {reviewIndex + 1} of {reviewItems.length}
                      </span>

                      <button
                        disabled={reviewIndex >= reviewItems.length - 1}
                        onClick={() => setReviewIndex((idx) => Math.min(reviewItems.length - 1, idx + 1))}
                        style={{
                          background: 'transparent',
                          border: 'none',
                          color: reviewIndex >= reviewItems.length - 1 ? '#555' : 'var(--tg-theme-button-color, #0a84ff)',
                          cursor: reviewIndex >= reviewItems.length - 1 ? 'not-allowed' : 'pointer',
                          display: 'flex',
                          alignItems: 'center',
                          fontSize: '12px',
                          fontWeight: 600,
                        }}
                      >
                        Next <ChevronRight size={16} />
                      </button>
                    </div>
                  </div>
                );
              })()}

              {/* Batch review buttons */}
              <div style={{ display: 'flex', gap: '8px', marginTop: '4px' }}>
                <button
                  disabled={isApplyingTrack}
                  onClick={handleApplyAllReview}
                  style={{
                    flex: 1,
                    padding: '10px',
                    borderRadius: '8px',
                    border: 'none',
                    backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
                    color: 'var(--tg-theme-button-text-color, #fff)',
                    fontSize: '13px',
                    fontWeight: 700,
                    cursor: 'pointer',
                  }}
                >
                  Accept All Remaining ({reviewItems.length})
                </button>

                <button
                  disabled={isApplyingTrack}
                  onClick={handleDiscardAllReview}
                  style={{
                    padding: '10px 14px',
                    borderRadius: '8px',
                    border: '1px solid rgba(255, 255, 255, 0.1)',
                    backgroundColor: 'transparent',
                    color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                    fontSize: '13px',
                    cursor: 'pointer',
                  }}
                >
                  Discard All
                </button>
              </div>
            </div>
          )}

          {/* STEP 4: Done / Summary */}
          {step === 'done' && (
            <div
              style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                padding: '24px 10px',
                textAlign: 'center',
                gap: '16px',
              }}
            >
              <div
                style={{
                  width: '56px',
                  height: '56px',
                  borderRadius: '50%',
                  backgroundColor: 'rgba(48, 209, 88, 0.15)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  color: '#30d158',
                }}
              >
                <CheckCircle2 size={32} />
              </div>

              <div>
                <div style={{ fontSize: '18px', fontWeight: 700, color: 'var(--tg-theme-text-color, #fff)' }}>
                  Bulk Re-Search Completed
                </div>
                {report && (
                  <div style={{ fontSize: '13px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', marginTop: '6px' }}>
                    Successfully enriched <b>{report.applied_count}</b> track(s) in {report.elapsed_seconds}s.
                  </div>
                )}
              </div>

              {report && (
                <div
                  style={{
                    width: '100%',
                    padding: '14px',
                    borderRadius: '12px',
                    backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                    border: '1px solid rgba(255, 255, 255, 0.08)',
                    display: 'grid',
                    gridTemplateColumns: '1fr 1fr',
                    gap: '10px',
                    textAlign: 'left',
                    fontSize: '13px',
                  }}
                >
                  <div>
                    <span style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>Total Processed:</span>
                    <div style={{ fontWeight: 700, color: '#fff' }}>{report.total_tracks}</div>
                  </div>
                  <div>
                    <span style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>Enriched:</span>
                    <div style={{ fontWeight: 700, color: '#30d158' }}>{report.applied_count}</div>
                  </div>
                  <div>
                    <span style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>Skipped / Unchanged:</span>
                    <div style={{ fontWeight: 700, color: '#8e8e93' }}>{report.skipped_count}</div>
                  </div>
                  <div>
                    <span style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>Errors / Failed:</span>
                    <div style={{ fontWeight: 700, color: report.failed_count > 0 ? '#ff453a' : '#8e8e93' }}>
                      {report.failed_count}
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Footer Actions */}
        <div
          style={{
            padding: '14px 20px',
            borderTop: '1px solid rgba(255, 255, 255, 0.08)',
            display: 'flex',
            justifyContent: 'flex-end',
            gap: '10px',
          }}
        >
          {step === 'scope' && (
            <>
              <button
                disabled={isPlanning}
                onClick={handlePlanDryRun}
                style={{
                  padding: '9px 16px',
                  borderRadius: '10px',
                  border: '1px solid rgba(255, 255, 255, 0.1)',
                  backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                  color: 'var(--tg-theme-text-color, #fff)',
                  fontSize: '13px',
                  fontWeight: 600,
                  cursor: isPlanning ? 'not-allowed' : 'pointer',
                }}
              >
                {isPlanning ? 'Planning...' : 'Plan Dry-Run'}
              </button>
              <button
                onClick={handleExecute}
                style={{
                  padding: '9px 20px',
                  borderRadius: '10px',
                  border: 'none',
                  backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
                  color: 'var(--tg-theme-button-text-color, #fff)',
                  fontSize: '13px',
                  fontWeight: 700,
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                }}
              >
                <span>Execute Re-Search</span>
                <ArrowRight size={15} />
              </button>
            </>
          )}

          {step === 'done' && (
            <button
              onClick={onClose}
              style={{
                padding: '9px 24px',
                borderRadius: '10px',
                border: 'none',
                backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
                color: 'var(--tg-theme-button-text-color, #fff)',
                fontSize: '14px',
                fontWeight: 700,
                cursor: 'pointer',
              }}
            >
              Done
            </button>
          )}
        </div>
      </div>
    </div>
  );
};

