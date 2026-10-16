import React, { useState } from 'react';
import { ExternalLink, Check, Search, AlertTriangle, Loader2, Edit3 } from 'lucide-react';
import { CandidateItem } from '../../types/queue';

interface DisambiguationCardProps {
  jobId: string;
  candidates: CandidateItem[];
  onSelect: (index: number) => Promise<void>;
  onManualSearch: (query: string) => Promise<void>;
  onOpenEdit?: (candidate?: CandidateItem) => void;
  disabled?: boolean;
  isReadOnly?: boolean;
}

export const DisambiguationCard: React.FC<DisambiguationCardProps> = ({
  jobId: _jobId,
  candidates,
  onSelect,
  onManualSearch,
  onOpenEdit,
  disabled = false,
  isReadOnly = false,
}) => {
  const [selectedIdx, setSelectedIdx] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [manualQuery, setManualQuery] = useState<string>('');
  const [searching, setSearching] = useState<boolean>(false);
  const [showSearchBox, setShowSearchBox] = useState<boolean>(false);

  const handleSelectClick = async (idx: number) => {
    if (disabled || submitting) return;
    setSelectedIdx(idx);
    setSubmitting(true);
    try {
      await onSelect(idx);
    } finally {
      setSubmitting(false);
      setSelectedIdx(null);
    }
  };

  const handleManualSearchSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!manualQuery.trim() || disabled || searching) return;
    setSearching(true);
    try {
      await onManualSearch(manualQuery.trim());
      setManualQuery('');
      setShowSearchBox(false);
    } finally {
      setSearching(false);
    }
  };

  return (
    <div
      style={{
        backgroundColor: 'rgba(255, 214, 10, 0.08)',
        border: '1px solid rgba(255, 214, 10, 0.3)',
        borderRadius: '10px',
        padding: '12px',
        display: 'flex',
        flexDirection: 'column',
        gap: '10px',
      }}
    >
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          <AlertTriangle size={16} color="#ffd60a" />
          <span style={{ fontSize: '13px', fontWeight: 700, color: '#ffd60a' }}>
            Release Disambiguation Required
          </span>
        </div>
        {!isReadOnly && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            {onOpenEdit && (
              <button
                type="button"
                onClick={() => onOpenEdit()}
                style={{
                  background: 'none',
                  border: 'none',
                  color: 'var(--tg-theme-button-color, #0a84ff)',
                  fontSize: '12px',
                  cursor: 'pointer',
                  padding: '2px 4px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '4px',
                }}
              >
                <Edit3 size={12} />
                <span>Edit Tags</span>
              </button>
            )}
            <button
              type="button"
              onClick={() => setShowSearchBox(!showSearchBox)}
              style={{
                background: 'none',
                border: 'none',
                color: 'var(--tg-theme-button-color, #0a84ff)',
                fontSize: '12px',
                cursor: 'pointer',
                padding: '2px 4px',
                display: 'flex',
                alignItems: 'center',
                gap: '4px',
              }}
            >
              <Search size={12} />
              <span>{showSearchBox ? 'Close Search' : 'Manual Search'}</span>
            </button>
          </div>
        )}
      </div>

      <div style={{ fontSize: '12px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
        {isReadOnly
          ? 'Multiple release matches were found. Disambiguation selection is restricted to the job submitter or moderators.'
          : 'Multiple release matches were found. Select the accurate release below or refine via search:'}
      </div>

      {/* Manual Search Input Form */}
      {showSearchBox && (
        <form
          onSubmit={handleManualSearchSubmit}
          style={{ display: 'flex', gap: '6px', marginTop: '2px' }}
        >
          <input
            type="text"
            value={manualQuery}
            onChange={(e) => setManualQuery(e.target.value)}
            placeholder="Search by artist, title, MBID, or URL..."
            style={{
              flex: 1,
              backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
              border: '1px solid var(--tg-theme-subtitle-text-color, #3a3a3c)',
              borderRadius: '6px',
              color: 'var(--tg-theme-text-color, #ffffff)',
              padding: '6px 10px',
              fontSize: '12px',
            }}
          />
          <button
            type="submit"
            disabled={searching || !manualQuery.trim()}
            style={{
              backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
              color: '#ffffff',
              border: 'none',
              borderRadius: '6px',
              padding: '6px 12px',
              fontSize: '12px',
              fontWeight: 600,
              cursor: searching || !manualQuery.trim() ? 'not-allowed' : 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
            }}
          >
            {searching ? <Loader2 size={12} className="spin" /> : <Search size={12} />}
            <span>Search</span>
          </button>
        </form>
      )}

      {/* Candidate List */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        {candidates.map((cand, idx) => {
          const pct = Math.round(cand.confidence * 100);
          const isSelected = selectedIdx === idx;
          const badgeColor = pct >= 90 ? '#30d158' : pct >= 75 ? '#ffd60a' : '#ff9f0a';

          return (
            <div
              key={cand.index ?? idx}
              style={{
                backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                borderRadius: '8px',
                padding: '8px 10px',
                display: 'flex',
                flexDirection: 'column',
                gap: '6px',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <span
                    style={{
                      backgroundColor: badgeColor,
                      color: '#000000',
                      fontSize: '10px',
                      fontWeight: 800,
                      padding: '1px 6px',
                      borderRadius: '4px',
                    }}
                  >
                    {pct}% MATCH
                  </span>
                  <span style={{ fontSize: '12px', fontWeight: 700 }}>
                    {cand.artist} — {cand.title}
                  </span>
                </div>

                {cand.release_mbid && (
                  <a
                    href={`https://musicbrainz.org/release/${cand.release_mbid}`}
                    target="_blank"
                    rel="noreferrer"
                    style={{
                      color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                      fontSize: '11px',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '2px',
                      textDecoration: 'none',
                    }}
                  >
                    <span>MB</span>
                    <ExternalLink size={10} />
                  </a>
                )}
              </div>

              <div
                style={{
                  fontSize: '11px',
                  color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                  display: 'flex',
                  gap: '8px',
                }}
              >
                <span>📀 {cand.album || 'Unknown Album'}</span>
                {cand.year && <span>({cand.year})</span>}
                {cand.release_type && <span>[{cand.release_type}]</span>}
                {cand.disambiguation && <span>• {cand.disambiguation}</span>}
              </div>

              {!isReadOnly && (
                <div style={{ display: 'flex', gap: '6px', marginTop: '2px' }}>
                  <button
                    type="button"
                    onClick={() => handleSelectClick(idx)}
                    disabled={disabled || submitting}
                    style={{
                      flex: 1,
                      backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
                      color: '#ffffff',
                      border: 'none',
                      borderRadius: '6px',
                      padding: '6px 10px',
                      fontSize: '12px',
                      fontWeight: 600,
                      cursor: disabled || submitting ? 'not-allowed' : 'pointer',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      gap: '6px',
                    }}
                  >
                    {isSelected ? (
                      <>
                        <Loader2 size={13} className="spin" />
                        <span>Confirming...</span>
                      </>
                    ) : (
                      <>
                        <Check size={13} />
                        <span>Select This Release</span>
                      </>
                    )}
                  </button>
                  {onOpenEdit && (
                    <button
                      type="button"
                      onClick={() => onOpenEdit(cand)}
                      disabled={disabled || submitting}
                      style={{
                        backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
                        border: '1px solid var(--tg-theme-subtitle-text-color, #3a3a3c)',
                        color: 'var(--tg-theme-text-color, #ffffff)',
                        borderRadius: '6px',
                        padding: '6px 10px',
                        fontSize: '12px',
                        fontWeight: 600,
                        cursor: disabled || submitting ? 'not-allowed' : 'pointer',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        gap: '4px',
                        whiteSpace: 'nowrap',
                      }}
                      title="Edit tags before confirming"
                    >
                      <Edit3 size={12} />
                      <span>Edit &amp; Ingest</span>
                    </button>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};

