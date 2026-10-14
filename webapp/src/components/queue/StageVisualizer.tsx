import React from 'react';
import {
  FolderArchive,
  Fingerprint,
  CopyCheck,
  Disc3,
  AlignLeft,
  CheckCircle2,
  AlertCircle,
  Clock,
} from 'lucide-react';
import { QueueJobState } from '../../types/queue';

interface StageVisualizerProps {
  state: QueueJobState;
  hasSyncedLyrics?: boolean;
}

interface StepConfig {
  id: string;
  label: string;
  icon: React.ReactNode;
}

const STAGES: StepConfig[] = [
  { id: 'extract', label: 'Staging', icon: <FolderArchive size={14} /> },
  { id: 'fp', label: 'Fingerprint', icon: <Fingerprint size={14} /> },
  { id: 'dedup', label: 'Dedup', icon: <CopyCheck size={14} /> },
  { id: 'meta', label: 'Metadata', icon: <Disc3 size={14} /> },
  { id: 'lyrics', label: 'Lyrics', icon: <AlignLeft size={14} /> },
  { id: 'store', label: 'Filed', icon: <CheckCircle2 size={14} /> },
];

function getStageIndex(state: QueueJobState): number {
  switch (state) {
    case 'QUEUED':
    case 'PAUSED':
    case 'PENDING':
      return -1;
    case 'PROCESSING':
    case 'EXTRACTING':
      return 0;
    case 'FINGERPRINTING':
      return 1;
    case 'ENRICHING_METADATA':
    case 'AWAITING_CONFIRMATION':
    case 'AWAITING_INPUT':
      return 3;
    case 'FETCHING_LYRICS':
      return 4;
    case 'ORGANIZING':
      return 5;
    case 'COMPLETED':
      return 6;
    case 'DUPLICATE_REJECTED':
      return 2;
    case 'CANCELLED':
    case 'FAILED':
      return -2;
    default:
      return 0;
  }
}

export const StageVisualizer: React.FC<StageVisualizerProps> = ({ state }) => {
  const currentIndex = getStageIndex(state);
  const isFailed = state === 'FAILED' || state === 'DUPLICATE_REJECTED' || state === 'CANCELLED';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
      {/* Progress Track */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          position: 'relative',
          padding: '4px 0',
        }}
      >
        {/* Connecting Line */}
        <div
          style={{
            position: 'absolute',
            top: '50%',
            left: '12px',
            right: '12px',
            height: '2px',
            backgroundColor: 'var(--tg-theme-secondary-bg-color, #3a3a3c)',
            zIndex: 1,
            transform: 'translateY(-50%)',
          }}
        />

        {STAGES.map((step, idx) => {
          let isCompleted = false;
          let isActive = false;
          let isError = false;

          if (isFailed) {
            if (state === 'DUPLICATE_REJECTED' && idx === 2) {
              isError = true;
            } else if (state === 'FAILED' && idx === Math.max(0, currentIndex)) {
              isError = true;
            }
          } else if (currentIndex > idx) {
            isCompleted = true;
          } else if (currentIndex === idx) {
            isActive = true;
          }

          let bgColor = 'var(--tg-theme-secondary-bg-color, #2c2c2e)';
          let borderColor = 'var(--tg-theme-subtitle-text-color, #636366)';
          let iconColor = 'var(--tg-theme-subtitle-text-color, #8e8e93)';

          if (isCompleted) {
            bgColor = 'var(--tg-theme-button-color, #0a84ff)';
            borderColor = 'var(--tg-theme-button-color, #0a84ff)';
            iconColor = '#ffffff';
          } else if (isActive) {
            bgColor = 'rgba(10, 132, 255, 0.2)';
            borderColor = 'var(--tg-theme-button-color, #0a84ff)';
            iconColor = 'var(--tg-theme-button-color, #0a84ff)';
          } else if (isError) {
            bgColor = 'rgba(255, 69, 58, 0.2)';
            borderColor = 'var(--tg-theme-destructive-text-color, #ff453a)';
            iconColor = 'var(--tg-theme-destructive-text-color, #ff453a)';
          }

          return (
            <div
              key={step.id}
              style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: '4px',
                zIndex: 2,
              }}
            >
              <div
                style={{
                  width: '24px',
                  height: '24px',
                  borderRadius: '50%',
                  backgroundColor: bgColor,
                  border: `2px solid ${borderColor}`,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  color: iconColor,
                  transition: 'all 0.2s ease',
                  boxShadow: isActive ? '0 0 8px rgba(10, 132, 255, 0.5)' : 'none',
                }}
              >
                {isError ? (
                  <AlertCircle size={13} />
                ) : isCompleted ? (
                  <CheckCircle2 size={13} />
                ) : isActive ? (
                  step.icon
                ) : (
                  <span style={{ fontSize: '10px', fontWeight: 600 }}>{idx + 1}</span>
                )}
              </div>
              <span
                style={{
                  fontSize: '10px',
                  fontWeight: isActive ? 700 : 500,
                  color: isActive
                    ? 'var(--tg-theme-text-color, #ffffff)'
                    : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                }}
              >
                {step.label}
              </span>
            </div>
          );
        })}
      </div>

      {/* State Label & Subtext */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          fontSize: '11px',
          color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
          backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
          padding: '4px 8px',
          borderRadius: '6px',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
          <Clock size={12} />
          <span>Stage: {state.replace(/_/g, ' ')}</span>
        </div>
        <div>
          {state === 'COMPLETED' && <span style={{ color: '#30d158' }}>Ready in Library</span>}
          {state === 'AWAITING_CONFIRMATION' && (
            <span style={{ color: '#ffd60a' }}>Confirmation Needed</span>
          )}
          {state === 'FAILED' && <span style={{ color: '#ff453a' }}>Ingestion Failed</span>}
        </div>
      </div>
    </div>
  );
};

