import React, { useState } from 'react';
import { X, Music2, Image, Mic2, Sliders, Bell, Check } from 'lucide-react';
import { hapticFeedback } from '@telegram-apps/sdk-react';
import { useAudioPlayer } from '../../context/AudioPlayerContext';
import { apiFetch } from '../../lib/auth';
import { TrackItem } from '../../types/music';
import { ProposalResponse } from '../../types/studio';
import { EditMetadataModal } from './EditMetadataModal';
import { CoverUploaderModal } from './CoverUploaderModal';
import { LrcEditorModal } from './LrcEditorModal';
import { DryRunConfirmModal } from './DryRunConfirmModal';

interface SongStudioModalProps {
  isOpen: boolean;
  track: TrackItem | null;
  onClose: () => void;
  onTrackUpdated: (updated: TrackItem) => void;
}

type StudioTab = 'metadata' | 'cover' | 'lyrics';

export const SongStudioModal: React.FC<SongStudioModalProps> = ({
  isOpen,
  track,
  onClose,
  onTrackUpdated,
}) => {
  const [activeTab, setActiveTab] = useState<StudioTab>('metadata');
  const [pendingProposal, setPendingProposal] = useState<ProposalResponse | null>(null);
  const [isApplying, setIsApplying] = useState<boolean>(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const { isArtistFollowed, toggleFollowArtist } = useAudioPlayer();

  if (!isOpen || !track) return null;

  const handlePropose = (proposal: ProposalResponse) => {
    setPendingProposal(proposal);
  };

  const handleConfirmApply = async (
    customProposal?: import('../../types/studio').SongEditProposalPayload,
    acceptedFields?: string[]
  ) => {
    const proposalToApply = customProposal || pendingProposal?.proposal;
    if (!proposalToApply) return;
    setIsApplying(true);
    setApplyError(null);
    try {
      const payload: Record<string, unknown> = { proposal: proposalToApply };
      if (acceptedFields) {
        payload.accepted_fields = acceptedFields;
      }
      const res = await apiFetch(`/api/v1/app/tracks/${track.id}/apply-edit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || `Commit failed (${res.status})`);
      }

      const outcome = await res.json();
      const updatedTrack: TrackItem = outcome.track;

      // Haptic feedback
      try {
        if (hapticFeedback.notificationOccurred.isAvailable()) {
          hapticFeedback.notificationOccurred('success');
        }
      } catch {
        // Ignored outside Telegram
      }

      onTrackUpdated(updatedTrack);
      setPendingProposal(null);
      onClose();
    } catch (err: unknown) {
      setApplyError((err as Error).message || 'Failed to apply changes.');
    } finally {
      setIsApplying(false);
    }
  };

  return (
    <>
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
                  Song Studio
                </div>
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '6px',
                    fontSize: '12px',
                    color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                    maxWidth: '300px',
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}
                >
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {track.title} • {track.artist}
                  </span>
                  <button
                    onClick={() => toggleFollowArtist(track.artist)}
                    title={isArtistFollowed(track.artist) ? `Unfollow ${track.artist}` : `Follow ${track.artist}`}
                    style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: '3px',
                      padding: '1px 6px',
                      borderRadius: '10px',
                      border: 'none',
                      fontSize: '10px',
                      fontWeight: 600,
                      backgroundColor: isArtistFollowed(track.artist)
                        ? 'rgba(48, 209, 88, 0.15)'
                        : 'rgba(255, 255, 255, 0.1)',
                      color: isArtistFollowed(track.artist) ? '#30d158' : 'var(--tg-theme-button-color, #0a84ff)',
                      cursor: 'pointer',
                      flexShrink: 0,
                    }}
                  >
                    {isArtistFollowed(track.artist) ? <Check size={10} strokeWidth={2.5} /> : <Bell size={10} />}
                    <span>{isArtistFollowed(track.artist) ? 'Following' : 'Follow'}</span>
                  </button>
                </div>
              </div>
            </div>
            <button
              onClick={onClose}
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

          {/* Segmented Studio Tabs */}
          <div
            style={{
              display: 'flex',
              backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
              padding: '4px',
              margin: '12px 18px 0 18px',
              borderRadius: '10px',
              gap: '4px',
            }}
          >
            <button
              onClick={() => setActiveTab('metadata')}
              style={{
                flex: 1,
                padding: '8px',
                borderRadius: '8px',
                border: 'none',
                backgroundColor: activeTab === 'metadata' ? 'var(--tg-theme-bg-color, #1c1c1e)' : 'transparent',
                color: activeTab === 'metadata' ? 'var(--tg-theme-button-color, #0a84ff)' : '#8e8e93',
                fontSize: '13px',
                fontWeight: 600,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: '6px',
                cursor: 'pointer',
              }}
            >
              <Music2 size={15} />
              <span>Metadata</span>
            </button>

            <button
              onClick={() => setActiveTab('cover')}
              style={{
                flex: 1,
                padding: '8px',
                borderRadius: '8px',
                border: 'none',
                backgroundColor: activeTab === 'cover' ? 'var(--tg-theme-bg-color, #1c1c1e)' : 'transparent',
                color: activeTab === 'cover' ? 'var(--tg-theme-button-color, #0a84ff)' : '#8e8e93',
                fontSize: '13px',
                fontWeight: 600,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: '6px',
                cursor: 'pointer',
              }}
            >
              <Image size={15} />
              <span>Cover Art</span>
            </button>

            <button
              onClick={() => setActiveTab('lyrics')}
              style={{
                flex: 1,
                padding: '8px',
                borderRadius: '8px',
                border: 'none',
                backgroundColor: activeTab === 'lyrics' ? 'var(--tg-theme-bg-color, #1c1c1e)' : 'transparent',
                color: activeTab === 'lyrics' ? 'var(--tg-theme-button-color, #0a84ff)' : '#8e8e93',
                fontSize: '13px',
                fontWeight: 600,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: '6px',
                cursor: 'pointer',
              }}
            >
              <Mic2 size={15} />
              <span>Lyrics</span>
            </button>
          </div>

          {/* Tab View Body */}
          <div style={{ padding: '16px 18px', overflowY: 'auto' }}>
            {activeTab === 'metadata' && (
              <EditMetadataModal
                track={track}
                onPropose={handlePropose}
                isProposing={false}
                onCancel={onClose}
              />
            )}
            {activeTab === 'cover' && (
              <CoverUploaderModal
                track={track}
                onPropose={handlePropose}
                isProposing={false}
                onCancel={onClose}
              />
            )}
            {activeTab === 'lyrics' && (
              <LrcEditorModal
                track={track}
                onPropose={handlePropose}
                isProposing={false}
                onCancel={onClose}
              />
            )}
          </div>
        </div>
      </div>

      {/* Mandatory Dry Run Modal */}
      <DryRunConfirmModal
        isOpen={Boolean(pendingProposal)}
        onClose={() => setPendingProposal(null)}
        proposalData={pendingProposal}
        onConfirmApply={handleConfirmApply}
        isApplying={isApplying}
        error={applyError}
      />
    </>
  );
};

