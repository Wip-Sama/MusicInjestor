import React, { useState } from 'react';
import {
  FileAudio,
  Archive,
  Pause,
  Play,
  X,
  AlertCircle,
  Copy,
  Check,
  FolderCheck,
  Edit3,
} from 'lucide-react';
import { QueueJob, CandidateItem } from '../../types/queue';
import { UserProfile, canResolveOthersJobs } from '../../lib/auth';
import { StageVisualizer } from './StageVisualizer';
import { DisambiguationCard } from './DisambiguationCard';

interface JobCardProps {
  job: QueueJob;
  onPause: (jobId: string) => Promise<void>;
  onResume: (jobId: string) => Promise<void>;
  onCancel: (jobId: string) => Promise<void>;
  onSelectCandidate: (jobId: string, candidateIndex: number) => Promise<void>;
  onManualSearch: (jobId: string, query: string) => Promise<void>;
  onOpenEdit?: (job: QueueJob, candidate?: CandidateItem) => void;
  profile?: UserProfile | null;
}

function formatBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

export const JobCard: React.FC<JobCardProps> = ({
  job,
  onPause,
  onResume,
  onCancel,
  onSelectCandidate,
  onManualSearch,
  onOpenEdit,
  profile,
}) => {
  const [copied, setCopied] = useState<boolean>(false);
  const [actionLoading, setActionLoading] = useState<boolean>(false);

  const isOwnJob = !job.user_id || job.user_id === profile?.telegram_id;
  const canControl = isOwnJob || (profile ? canResolveOthersJobs(profile) : false);

  const isArchive = job.source_type.includes('ARCHIVE');
  const isPending =
    job.state === 'PROCESSING' ||
    job.state === 'EXTRACTING' ||
    job.state === 'FINGERPRINTING' ||
    job.state === 'ENRICHING_METADATA' ||
    job.state === 'FETCHING_LYRICS' ||
    job.state === 'ORGANIZING';
  const isQueued = job.state === 'QUEUED';
  const isPaused = job.state === 'PAUSED';
  const isAwaiting = job.state === 'AWAITING_CONFIRMATION';
  const isCompleted = job.state === 'COMPLETED';
  const isFailed = job.state === 'FAILED' || job.state === 'DUPLICATE_REJECTED';
  const isCancelled = job.state === 'CANCELLED';

  const copyJobId = () => {
    navigator.clipboard.writeText(job.job_id);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const handleAction = async (fn: (id: string) => Promise<void>) => {
    if (actionLoading) return;
    setActionLoading(true);
    try {
      await fn(job.job_id);
    } finally {
      setActionLoading(false);
    }
  };

  // State Badge Mode
  let badgeText: string = job.state.replace(/_/g, ' ');
  let badgeBg = 'var(--tg-theme-secondary-bg-color, #2c2c2e)';
  let badgeColor = 'var(--tg-theme-subtitle-text-color, #8e8e93)';

  if (isQueued) {
    badgeText = job.queue_position > 0 ? `Queued #${job.queue_position}` : 'Queued';
    badgeBg = 'rgba(10, 132, 255, 0.15)';
    badgeColor = 'var(--tg-theme-button-color, #0a84ff)';
  } else if (isPending) {
    badgeBg = 'rgba(10, 132, 255, 0.2)';
    badgeColor = 'var(--tg-theme-button-color, #0a84ff)';
  } else if (isAwaiting) {
    badgeText = 'Needs Confirmation';
    badgeBg = 'rgba(255, 214, 10, 0.2)';
    badgeColor = '#ffd60a';
  } else if (isPaused) {
    badgeBg = 'rgba(255, 159, 10, 0.2)';
    badgeColor = '#ff9f0a';
  } else if (isCompleted) {
    badgeText = 'Ingested';
    badgeBg = 'rgba(48, 209, 88, 0.2)';
    badgeColor = '#30d158';
  } else if (isFailed) {
    badgeBg = 'rgba(255, 69, 58, 0.2)';
    badgeColor = '#ff453a';
  } else if (isCancelled) {
    badgeBg = 'rgba(142, 142, 147, 0.2)';
    badgeColor = '#8e8e93';
  }

  return (
    <div
      style={{
        backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
        border: '1px solid var(--tg-theme-secondary-bg-color, #2c2c2e)',
        borderRadius: '12px',
        padding: '12px',
        display: 'flex',
        flexDirection: 'column',
        gap: '10px',
        boxShadow: '0 1px 4px rgba(0,0,0,0.15)',
      }}
    >
      {/* Top Header: File info & State badge */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '8px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
          <div
            style={{
              width: '32px',
              height: '32px',
              borderRadius: '8px',
              backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexShrink: 0,
              color: isArchive ? '#ff9f0a' : 'var(--tg-theme-button-color, #0a84ff)',
            }}
          >
            {isArchive ? <Archive size={18} /> : <FileAudio size={18} />}
          </div>
          <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column' }}>
            <span
              style={{
                fontSize: '13px',
                fontWeight: 600,
                color: 'var(--tg-theme-text-color, #ffffff)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
              title={job.original_filename}
            >
              {job.original_filename || 'Audio Payload'}
            </span>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
              <span>{formatBytes(job.file_size_bytes)}</span>
              <span>•</span>
              <button
                onClick={copyJobId}
                style={{
                  background: 'none',
                  border: 'none',
                  padding: 0,
                  color: 'inherit',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '2px',
                  fontFamily: 'monospace',
                }}
              >
                <span>{job.job_id.slice(0, 8)}</span>
                {copied ? <Check size={10} color="#30d158" /> : <Copy size={10} />}
              </button>
            </div>
          </div>
        </div>

        {/* State Badge */}
        <span
          style={{
            backgroundColor: badgeBg,
            color: badgeColor,
            fontSize: '11px',
            fontWeight: 700,
            padding: '2px 8px',
            borderRadius: '6px',
            whiteSpace: 'nowrap',
          }}
        >
          {badgeText}
        </span>
      </div>

      {/* Stage Progress Visualizer for Active/Processing/Queued */}
      {(isPending || isQueued || isPaused) && <StageVisualizer state={job.state} />}

      {/* Confirmation Disambiguation Card */}
      {isAwaiting && (
        <DisambiguationCard
          jobId={job.job_id}
          candidates={job.candidates}
          onSelect={(idx) => onSelectCandidate(job.job_id, idx)}
          onManualSearch={(query) => onManualSearch(job.job_id, query)}
          onOpenEdit={(cand) => onOpenEdit?.(job, cand)}
          disabled={actionLoading}
          isReadOnly={!canControl}
        />
      )}

      {/* Completed Banner */}
      {isCompleted && (
        <div
          style={{
            backgroundColor: 'rgba(48, 209, 88, 0.1)',
            border: '1px solid rgba(48, 209, 88, 0.25)',
            borderRadius: '8px',
            padding: '8px 10px',
            fontSize: '12px',
            color: '#30d158',
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
          }}
        >
          <FolderCheck size={15} />
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {job.final_path || 'Successfully filed to library.'}
          </span>
        </div>
      )}

      {/* Error / Rejection Banner */}
      {isFailed && (
        <div
          style={{
            backgroundColor: 'rgba(255, 69, 58, 0.1)',
            border: '1px solid rgba(255, 69, 58, 0.25)',
            borderRadius: '8px',
            padding: '8px 10px',
            fontSize: '12px',
            color: '#ff453a',
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
          }}
        >
          <AlertCircle size={15} />
          <span>{job.error_message || (job.state === 'DUPLICATE_REJECTED' ? 'Duplicate track already exists in catalog.' : 'Ingestion failed.')}</span>
        </div>
      )}

      {/* Job Control Action Buttons */}
      {canControl && (isQueued || isPending || isPaused || isAwaiting) && (
        <div style={{ display: 'flex', gap: '6px', justifyContent: 'flex-end', paddingTop: '2px' }}>
          {/* Edit Tags Button */}
          {onOpenEdit && (
            <button
              type="button"
              onClick={() => onOpenEdit(job)}
              disabled={actionLoading}
              style={{
                backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                color: 'var(--tg-theme-text-color, #ffffff)',
                border: '1px solid var(--tg-theme-subtitle-text-color, #3a3a3c)',
                borderRadius: '6px',
                padding: '5px 10px',
                fontSize: '11px',
                fontWeight: 600,
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                gap: '4px',
              }}
            >
              <Edit3 size={12} />
              <span>Edit Tags</span>
            </button>
          )}

          {/* Pause Button */}
          {(isQueued || isPending) && (
            <button
              onClick={() => handleAction(onPause)}
              disabled={actionLoading}
              style={{
                backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                color: 'var(--tg-theme-text-color, #ffffff)',
                border: 'none',
                borderRadius: '6px',
                padding: '5px 10px',
                fontSize: '11px',
                fontWeight: 600,
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                gap: '4px',
              }}
            >
              <Pause size={12} />
              <span>Pause</span>
            </button>
          )}

          {/* Resume Button */}
          {isPaused && (
            <button
              onClick={() => handleAction(onResume)}
              disabled={actionLoading}
              style={{
                backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
                color: '#ffffff',
                border: 'none',
                borderRadius: '6px',
                padding: '5px 10px',
                fontSize: '11px',
                fontWeight: 600,
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                gap: '4px',
              }}
            >
              <Play size={12} />
              <span>Resume</span>
            </button>
          )}

          {/* Cancel Button */}
          <button
            onClick={() => handleAction(onCancel)}
            disabled={actionLoading}
            style={{
              backgroundColor: 'rgba(255, 69, 58, 0.15)',
              color: 'var(--tg-theme-destructive-text-color, #ff453a)',
              border: 'none',
              borderRadius: '6px',
              padding: '5px 10px',
              fontSize: '11px',
              fontWeight: 600,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
            }}
          >
            <X size={12} />
            <span>Cancel</span>
          </button>
        </div>
      )}
    </div>
  );
};
