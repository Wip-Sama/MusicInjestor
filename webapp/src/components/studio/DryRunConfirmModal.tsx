import React, { useState, useEffect } from 'react';
import {
  ShieldAlert,
  ArrowRight,
  Check,
  X,
  FolderSync,
  Image,
  Mic2,
  Loader2,
  CheckSquare,
  Square,
} from 'lucide-react';
import { ProposalResponse, SongEditProposalPayload } from '../../types/studio';

interface DryRunConfirmModalProps {
  isOpen: boolean;
  onClose: () => void;
  proposalData: ProposalResponse | null;
  onConfirmApply: (
    customProposal?: SongEditProposalPayload,
    acceptedFields?: string[]
  ) => Promise<void>;
  isApplying: boolean;
  error: string | null;
}

export const DryRunConfirmModal: React.FC<DryRunConfirmModalProps> = ({
  isOpen,
  onClose,
  proposalData,
  onConfirmApply,
  isApplying,
  error,
}) => {
  if (!isOpen || !proposalData) return null;

  const changedDiffs = proposalData.diff.filter((d) => d.changed);
  const allChangedKeys: string[] = [
    ...changedDiffs.map((d) => d.field),
    ...(proposalData.has_lyrics_change ? ['lyrics'] : []),
    ...(proposalData.has_cover_change ? ['cover'] : []),
  ];

  const [selectedFields, setSelectedFields] = useState<Set<string>>(
    () => new Set(allChangedKeys)
  );

  useEffect(() => {
    setSelectedFields(new Set(allChangedKeys));
  }, [proposalData]);

  const toggleField = (field: string) => {
    setSelectedFields((prev) => {
      const next = new Set(prev);
      if (next.has(field)) {
        next.delete(field);
      } else {
        next.add(field);
      }
      return next;
    });
  };

  const selectAll = () => setSelectedFields(new Set(allChangedKeys));
  const deselectAll = () => setSelectedFields(new Set());
  const selectOnlyLyrics = () => setSelectedFields(new Set(['lyrics']));
  const selectOnlyMetadata = () => setSelectedFields(new Set(changedDiffs.map((d) => d.field)));

  const handleApplyClick = async () => {
    if (selectedFields.size === 0) return;
    const acceptedFields = Array.from(selectedFields);
    const sanitizedProposal: SongEditProposalPayload = {
      ...proposalData.proposal,
      accepted_fields: acceptedFields,
    };

    proposalData.diff.forEach((item) => {
      if (!selectedFields.has(item.field)) {
        if (item.field === 'title') sanitizedProposal.new_title = String(item.old ?? '');
        if (item.field === 'artist') sanitizedProposal.new_artist = String(item.old ?? '');
        if (item.field === 'album') sanitizedProposal.new_album = String(item.old ?? '');
        if (item.field === 'year') {
          sanitizedProposal.new_year = typeof item.old === 'number' ? item.old : null;
        }
        if (item.field === 'track_number') {
          sanitizedProposal.new_track_number = typeof item.old === 'number' ? item.old : 1;
        }
      }
    });

    if (!selectedFields.has('lyrics')) {
      sanitizedProposal.new_lyrics = null;
    }
    if (!selectedFields.has('cover')) {
      sanitizedProposal.new_cover_hex = null;
    }

    await onConfirmApply(sanitizedProposal, acceptedFields);
  };

  const isAllSelected = selectedFields.size === allChangedKeys.length && allChangedKeys.length > 0;
  const isNoneSelected = selectedFields.size === 0 && allChangedKeys.length > 0;
  const hasMetadataChanges = changedDiffs.length > 0;
  const hasLyricsChange = proposalData.has_lyrics_change;

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 500,
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
          maxWidth: '520px',
          maxHeight: '88vh',
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
            padding: '16px 20px',
            borderBottom: '1px solid var(--tg-theme-secondary-bg-color, #2c2c2e)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <div
              style={{
                width: '36px',
                height: '36px',
                borderRadius: '8px',
                backgroundColor: 'rgba(10, 132, 255, 0.15)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: 'var(--tg-theme-button-color, #0a84ff)',
              }}
            >
              <ShieldAlert size={20} />
            </div>
            <div>
              <div style={{ fontSize: '16px', fontWeight: 700, color: 'var(--tg-theme-text-color, #fff)' }}>
                Dry-Run Diff Preview
              </div>
              <div style={{ fontSize: '12px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                Accept all, accept selected changes, or discard
              </div>
            </div>
          </div>
          <button
            onClick={onClose}
            disabled={isApplying}
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

        {/* Scrollable Content */}
        <div
          style={{
            padding: '16px 20px',
            overflowY: 'auto',
            display: 'flex',
            flexDirection: 'column',
            gap: '14px',
          }}
        >
          {/* Policy Banner */}
          <div
            style={{
              backgroundColor: 'rgba(255, 159, 10, 0.1)',
              border: '1px solid rgba(255, 159, 10, 0.3)',
              borderRadius: '10px',
              padding: '10px 14px',
              fontSize: '12px',
              color: '#ffd60a',
              lineHeight: 1.4,
            }}
          >
            <b>Selective Acceptance:</b> You can accept all proposed updates, select only specific fields, or discard all changes.
          </div>

          {/* Quick Filter Pills */}
          {allChangedKeys.length > 1 && (
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
              <button
                type="button"
                onClick={selectAll}
                style={{
                  padding: '4px 10px',
                  borderRadius: '6px',
                  border: isAllSelected ? '1px solid #0a84ff' : '1px solid #3a3a3c',
                  backgroundColor: isAllSelected ? 'rgba(10, 132, 255, 0.15)' : 'transparent',
                  color: isAllSelected ? '#0a84ff' : '#8e8e93',
                  fontSize: '11px',
                  fontWeight: 600,
                  cursor: 'pointer',
                }}
              >
                Select All ({allChangedKeys.length})
              </button>
              <button
                type="button"
                onClick={deselectAll}
                style={{
                  padding: '4px 10px',
                  borderRadius: '6px',
                  border: '1px solid #3a3a3c',
                  backgroundColor: 'transparent',
                  color: '#8e8e93',
                  fontSize: '11px',
                  fontWeight: 600,
                  cursor: 'pointer',
                }}
              >
                Deselect All
              </button>
              {hasMetadataChanges && hasLyricsChange && (
                <>
                  <button
                    type="button"
                    onClick={selectOnlyLyrics}
                    style={{
                      padding: '4px 10px',
                      borderRadius: '6px',
                      border:
                        selectedFields.size === 1 && selectedFields.has('lyrics')
                          ? '1px solid #0a84ff'
                          : '1px solid #3a3a3c',
                      backgroundColor:
                        selectedFields.size === 1 && selectedFields.has('lyrics')
                          ? 'rgba(10, 132, 255, 0.15)'
                          : 'transparent',
                      color:
                        selectedFields.size === 1 && selectedFields.has('lyrics')
                          ? '#0a84ff'
                          : '#8e8e93',
                      fontSize: '11px',
                      fontWeight: 600,
                      cursor: 'pointer',
                    }}
                  >
                    Only Lyrics
                  </button>
                  <button
                    type="button"
                    onClick={selectOnlyMetadata}
                    style={{
                      padding: '4px 10px',
                      borderRadius: '6px',
                      border:
                        !selectedFields.has('lyrics') &&
                        !selectedFields.has('cover') &&
                        selectedFields.size > 0
                          ? '1px solid #0a84ff'
                          : '1px solid #3a3a3c',
                      backgroundColor:
                        !selectedFields.has('lyrics') &&
                        !selectedFields.has('cover') &&
                        selectedFields.size > 0
                          ? 'rgba(10, 132, 255, 0.15)'
                          : 'transparent',
                      color:
                        !selectedFields.has('lyrics') &&
                        !selectedFields.has('cover') &&
                        selectedFields.size > 0
                          ? '#0a84ff'
                          : '#8e8e93',
                      fontSize: '11px',
                      fontWeight: 600,
                      cursor: 'pointer',
                    }}
                  >
                    Only Metadata
                  </button>
                </>
              )}
            </div>
          )}

          {/* Diffs List */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <div
              style={{
                fontSize: '12px',
                fontWeight: 700,
                textTransform: 'uppercase',
                letterSpacing: '0.5px',
                color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
              }}
            >
              Metadata Tag Changes
            </div>
            {proposalData.diff.map((item) => {
              const isSelected = selectedFields.has(item.field);
              return (
                <div
                  key={item.field}
                  onClick={() => item.changed && toggleField(item.field)}
                  style={{
                    backgroundColor: !item.changed
                      ? 'var(--tg-theme-secondary-bg-color, #2c2c2e)'
                      : isSelected
                      ? 'rgba(10, 132, 255, 0.08)'
                      : 'rgba(255, 255, 255, 0.02)',
                    border: !item.changed
                      ? '1px solid transparent'
                      : isSelected
                      ? '1px solid rgba(10, 132, 255, 0.35)'
                      : '1px solid #3a3a3c',
                    borderRadius: '10px',
                    padding: '8px 12px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    gap: '12px',
                    cursor: item.changed ? 'pointer' : 'default',
                    opacity: item.changed && !isSelected ? 0.6 : 1,
                    transition: 'all 0.15s ease',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    {item.changed && (
                      <span style={{ color: isSelected ? '#0a84ff' : '#8e8e93' }}>
                        {isSelected ? <CheckSquare size={16} /> : <Square size={16} />}
                      </span>
                    )}
                    <div style={{ width: '65px', fontSize: '13px', fontWeight: 600, color: 'var(--tg-theme-text-color, #fff)' }}>
                      {item.label}
                    </div>
                  </div>
                  <div
                    style={{
                      flex: 1,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'flex-end',
                      gap: '8px',
                      fontSize: '13px',
                      overflow: 'hidden',
                    }}
                  >
                    {item.changed ? (
                      isSelected ? (
                        <>
                          <span
                            style={{
                              color: '#ff453a',
                              textDecoration: 'line-through',
                              whiteSpace: 'nowrap',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              maxWidth: '120px',
                            }}
                          >
                            {String(item.old ?? 'None')}
                          </span>
                          <ArrowRight size={14} color="var(--tg-theme-button-color, #0a84ff)" style={{ flexShrink: 0 }} />
                          <span
                            style={{
                              color: '#30d158',
                              fontWeight: 600,
                              whiteSpace: 'nowrap',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              maxWidth: '130px',
                            }}
                          >
                            {String(item.new ?? 'None')}
                          </span>
                        </>
                      ) : (
                        <span style={{ color: '#8e8e93', fontStyle: 'italic', fontSize: '12px' }}>
                          {String(item.old ?? 'None')} (kept original)
                        </span>
                      )
                    ) : (
                      <span style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', fontStyle: 'italic' }}>
                        {String(item.old ?? 'None')} (unchanged)
                      </span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>

          {/* Optional Attachments Indicators with selective checkboxes */}
          {(proposalData.has_cover_change || proposalData.has_lyrics_change) && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              <div
                style={{
                  fontSize: '12px',
                  fontWeight: 700,
                  textTransform: 'uppercase',
                  letterSpacing: '0.5px',
                  color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                }}
              >
                Lyrics &amp; Media Changes
              </div>

              {proposalData.has_lyrics_change && (
                <div
                  onClick={() => toggleField('lyrics')}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    padding: '10px 12px',
                    borderRadius: '10px',
                    backgroundColor: selectedFields.has('lyrics')
                      ? 'rgba(10, 132, 255, 0.12)'
                      : 'rgba(255, 255, 255, 0.02)',
                    border: selectedFields.has('lyrics')
                      ? '1px solid rgba(10, 132, 255, 0.35)'
                      : '1px solid #3a3a3c',
                    cursor: 'pointer',
                    opacity: selectedFields.has('lyrics') ? 1 : 0.6,
                    transition: 'all 0.15s ease',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <span style={{ color: selectedFields.has('lyrics') ? '#0a84ff' : '#8e8e93' }}>
                      {selectedFields.has('lyrics') ? <CheckSquare size={16} /> : <Square size={16} />}
                    </span>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', fontWeight: 600 }}>
                      <Mic2 size={15} color="#0a84ff" />
                      <span>Lyrics Updates</span>
                    </div>
                  </div>
                  <div style={{ fontSize: '12px', fontWeight: 600 }}>
                    {selectedFields.has('lyrics') ? (
                      <span style={{ color: '#30d158' }}>Update tags &amp; .lrc sidecar</span>
                    ) : (
                      <span style={{ color: '#8e8e93', fontStyle: 'italic' }}>Keep existing lyrics</span>
                    )}
                  </div>
                </div>
              )}

              {proposalData.has_cover_change && (
                <div
                  onClick={() => toggleField('cover')}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    padding: '10px 12px',
                    borderRadius: '10px',
                    backgroundColor: selectedFields.has('cover')
                      ? 'rgba(48, 209, 88, 0.12)'
                      : 'rgba(255, 255, 255, 0.02)',
                    border: selectedFields.has('cover')
                      ? '1px solid rgba(48, 209, 88, 0.35)'
                      : '1px solid #3a3a3c',
                    cursor: 'pointer',
                    opacity: selectedFields.has('cover') ? 1 : 0.6,
                    transition: 'all 0.15s ease',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <span style={{ color: selectedFields.has('cover') ? '#30d158' : '#8e8e93' }}>
                      {selectedFields.has('cover') ? <CheckSquare size={16} /> : <Square size={16} />}
                    </span>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', fontWeight: 600 }}>
                      <Image size={15} color="#30d158" />
                      <span>Album Cover Art</span>
                    </div>
                  </div>
                  <div style={{ fontSize: '12px', fontWeight: 600 }}>
                    {selectedFields.has('cover') ? (
                      <span style={{ color: '#30d158' }}>Embed new cover.jpg</span>
                    ) : (
                      <span style={{ color: '#8e8e93', fontStyle: 'italic' }}>Keep existing cover</span>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Filesystem Path Move Preview */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <div
              style={{
                fontSize: '12px',
                fontWeight: 700,
                textTransform: 'uppercase',
                letterSpacing: '0.5px',
                color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
              }}
            >
              <FolderSync size={14} />
              <span>Storage Location</span>
            </div>
            <div
              style={{
                backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                borderRadius: '10px',
                padding: '10px 12px',
                fontSize: '12px',
                fontFamily: 'monospace',
                display: 'flex',
                flexDirection: 'column',
                gap: '6px',
                overflowX: 'auto',
              }}
            >
              {proposalData.path_changed ? (
                <>
                  <div style={{ color: '#ff453a', wordBreak: 'break-all' }}>
                    <b>Old:</b> {proposalData.old_path}
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '4px', color: 'var(--tg-theme-button-color, #0a84ff)' }}>
                    <ArrowRight size={14} />
                    <span style={{ fontWeight: 700, fontSize: '11px' }}>RELOCATING FILE ON DISK</span>
                  </div>
                  <div style={{ color: '#30d158', wordBreak: 'break-all', fontWeight: 600 }}>
                    <b>New:</b> {proposalData.new_path}
                  </div>
                </>
              ) : (
                <div style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                  <b>In-Place Retag:</b> {proposalData.old_path}
                </div>
              )}
            </div>
          </div>

          {error && (
            <div
              style={{
                backgroundColor: 'rgba(255, 69, 58, 0.15)',
                border: '1px solid rgba(255, 69, 58, 0.4)',
                borderRadius: '8px',
                padding: '8px 12px',
                fontSize: '12px',
                color: '#ff453a',
              }}
            >
              {error}
            </div>
          )}
        </div>

        {/* Footer Actions: Discard Changes vs Accept All / Accept Selected */}
        <div
          style={{
            padding: '14px 20px',
            borderTop: '1px solid var(--tg-theme-secondary-bg-color, #2c2c2e)',
            display: 'flex',
            gap: '10px',
          }}
        >
          <button
            type="button"
            onClick={onClose}
            disabled={isApplying}
            style={{
              flex: 1,
              padding: '12px',
              borderRadius: '10px',
              border: 'none',
              backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
              color: '#ff453a',
              fontSize: '14px',
              fontWeight: 600,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '6px',
              cursor: 'pointer',
            }}
          >
            <X size={16} />
            <span>Discard Changes</span>
          </button>
          <button
            type="button"
            onClick={handleApplyClick}
            disabled={isApplying || isNoneSelected}
            style={{
              flex: 2,
              padding: '12px',
              borderRadius: '10px',
              border: 'none',
              backgroundColor: isNoneSelected ? '#3a3a3c' : 'var(--tg-theme-button-color, #0a84ff)',
              color: isNoneSelected ? '#8e8e93' : '#fff',
              fontSize: '14px',
              fontWeight: 700,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '6px',
              cursor: isApplying || isNoneSelected ? 'not-allowed' : 'pointer',
              opacity: isApplying ? 0.7 : 1,
            }}
          >
            {isApplying ? (
              <>
                <Loader2 size={16} className="animate-spin" />
                <span>Applying Changes...</span>
              </>
            ) : isAllSelected ? (
              <>
                <Check size={16} />
                <span>Accept All Changes</span>
              </>
            ) : isNoneSelected ? (
              <span>Select At Least 1 Change</span>
            ) : (
              <>
                <Check size={16} />
                <span>Accept Selected ({selectedFields.size}/{allChangedKeys.length})</span>
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
};
