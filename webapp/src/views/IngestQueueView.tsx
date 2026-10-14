import React, { useEffect, useState, useCallback } from 'react';
import {
  Activity,
  RefreshCw,
  Layers,
  Inbox,
  Radio,
  History,
  ListFilter,
  Lock,
} from 'lucide-react';
import { FileDropzone } from '../components/ingest/FileDropzone';
import { JobCard } from '../components/queue/JobCard';
import {
  fetchQueue,
  pauseJob,
  resumeJob,
  cancelJob,
  selectCandidate,
  manualSearch,
  connectQueueEvents,
} from '../lib/queueApi';
import { QueueJob, QueueResponse, QueueEvent } from '../types/queue';
import { UserProfile, canSubmitIngestion } from '../lib/auth';

interface IngestQueueViewProps {
  profile: UserProfile | null;
}

export const IngestQueueView: React.FC<IngestQueueViewProps> = ({ profile }) => {
  const canViewGlobal = profile?.role === 'MODERATOR' || profile?.role === 'ADMIN' || profile?.role === 'OWNER';

  const [queueData, setQueueData] = useState<QueueResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<'mine' | 'global'>('mine');
  const [filterMode, setFilterMode] = useState<'active' | 'all'>('active');
  const [sseConnected, setSseConnected] = useState<boolean>(false);

  const loadQueue = useCallback(async () => {
    try {
      const data = await fetchQueue(activeTab === 'mine');
      setQueueData(data);
      setError(null);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to load queue';
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [activeTab]);

  // Initial fetch and on activeTab change
  useEffect(() => {
    loadQueue();
  }, [loadQueue]);

  // Connect Server-Sent Events (SSE)
  useEffect(() => {
    const cleanup = connectQueueEvents(
      (event: QueueEvent) => {
        if (event.type === 'connected') {
          setSseConnected(true);
          return;
        }

        // Live update the job list when event has job data
        if (event.job) {
          const updated = event.job;
          setQueueData((prev) => {
            if (!prev) return prev;
            const exists = prev.jobs.some((j) => j.job_id === updated.job_id);
            const newJobs = exists
              ? prev.jobs.map((j) => (j.job_id === updated.job_id ? updated : j))
              : [updated, ...prev.jobs];

            return {
              ...prev,
              jobs: newJobs,
            };
          });
        } else {
          // Re-fetch snapshot on other events
          loadQueue();
        }
      },
      () => {
        setSseConnected(false);
      }
    );

    return () => {
      cleanup();
    };
  }, [loadQueue]);

  const hasActiveJobs = (queueData?.jobs || []).some(
    (j) =>
      j.state === 'QUEUED' ||
      j.state === 'PROCESSING' ||
      j.state === 'EXTRACTING' ||
      j.state === 'FINGERPRINTING' ||
      j.state === 'ENRICHING_METADATA' ||
      j.state === 'FETCHING_LYRICS' ||
      j.state === 'ORGANIZING'
  );

  // Fallback auto-polling safety net when active jobs exist or SSE is disconnected
  useEffect(() => {
    if (!hasActiveJobs && sseConnected) return;
    const interval = setInterval(() => {
      loadQueue();
    }, 3500);
    return () => clearInterval(interval);
  }, [hasActiveJobs, sseConnected, loadQueue]);

  const handleJobUploaded = (newJob: QueueJob) => {
    setQueueData((prev) => {
      if (!prev) return null;
      return {
        ...prev,
        jobs: [newJob, ...prev.jobs.filter((j) => j.job_id !== newJob.job_id)],
        queued_count: prev.queued_count + 1,
      };
    });
  };

  const handlePause = async (jobId: string) => {
    try {
      const updated = await pauseJob(jobId);
      setQueueData((prev) =>
        prev ? { ...prev, jobs: prev.jobs.map((j) => (j.job_id === jobId ? updated : j)) } : null
      );
      await loadQueue();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to pause job');
      await loadQueue();
    }
  };

  const handleResume = async (jobId: string) => {
    try {
      const updated = await resumeJob(jobId);
      setQueueData((prev) =>
        prev ? { ...prev, jobs: prev.jobs.map((j) => (j.job_id === jobId ? updated : j)) } : null
      );
      await loadQueue();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to resume job');
      await loadQueue();
    }
  };

  const handleCancel = async (jobId: string) => {
    try {
      const updated = await cancelJob(jobId);
      setQueueData((prev) =>
        prev ? { ...prev, jobs: prev.jobs.map((j) => (j.job_id === jobId ? updated : j)) } : null
      );
      await loadQueue();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to cancel job');
      await loadQueue();
    }
  };

  const handleSelectCandidate = async (jobId: string, candidateIndex: number) => {
    try {
      const updated = await selectCandidate(jobId, candidateIndex);
      setQueueData((prev) =>
        prev ? { ...prev, jobs: prev.jobs.map((j) => (j.job_id === jobId ? updated : j)) } : null
      );
      await loadQueue();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to select release candidate');
      await loadQueue();
    }
  };

  const handleManualSearch = async (jobId: string, query: string) => {
    try {
      const updated = await manualSearch(jobId, query);
      setQueueData((prev) =>
        prev ? { ...prev, jobs: prev.jobs.map((j) => (j.job_id === jobId ? updated : j)) } : null
      );
      await loadQueue();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Manual search failed');
      await loadQueue();
    }
  };

  const allJobs = queueData?.jobs || [];
  const filteredJobs = allJobs.filter((job) => {
    if (filterMode === 'active') {
      return (
        job.state === 'QUEUED' ||
        job.state === 'PAUSED' ||
        job.state === 'PROCESSING' ||
        job.state === 'EXTRACTING' ||
        job.state === 'FINGERPRINTING' ||
        job.state === 'ENRICHING_METADATA' ||
        job.state === 'AWAITING_CONFIRMATION' ||
        job.state === 'AWAITING_INPUT' ||
        job.state === 'FETCHING_LYRICS' ||
        job.state === 'ORGANIZING'
      );
    }
    return true;
  });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      {/* File Upload Dropzone or Locked Card */}
      {canSubmitIngestion(profile) ? (
        <FileDropzone onJobUploaded={handleJobUploaded} />
      ) : (
        <div
          style={{
            backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
            border: '1px dashed rgba(255, 159, 10, 0.4)',
            borderRadius: '12px',
            padding: '24px 16px',
            textAlign: 'center',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            gap: '8px',
          }}
        >
          <Lock size={30} color="#ff9f0a" />
          <div style={{ fontSize: '15px', fontWeight: 600 }}>Music Upload Locked</div>
          <div
            style={{
              fontSize: '12px',
              color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
              maxWidth: '380px',
              lineHeight: 1.4,
            }}
          >
            Your account role is <strong>{profile?.role || 'GUEST'}</strong>. Submitting audio tracks and archives requires <strong>UPLOADER</strong> permission.
          </div>
        </div>
      )}

      {/* Live Board Status Bar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
          borderRadius: '10px',
          padding: '8px 12px',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px' }}>
          <Activity size={16} color="var(--tg-theme-button-color, #0a84ff)" />
          <span>
            Worker: <strong>{queueData?.active_count || 0}</strong> active /{' '}
            {queueData?.concurrency_limit || 1} max
          </span>
          <span style={{ color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>•</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
            <Radio
              size={12}
              color={sseConnected ? '#30d158' : '#8e8e93'}
              className={sseConnected ? 'pulse' : ''}
            />
            <span style={{ color: sseConnected ? '#30d158' : '#8e8e93', fontSize: '11px' }}>
              {sseConnected ? 'Live' : 'Polling'}
            </span>
          </div>
        </div>

        <button
          onClick={loadQueue}
          disabled={loading}
          style={{
            background: 'none',
            border: 'none',
            color: 'var(--tg-theme-button-color, #0a84ff)',
            cursor: 'pointer',
            padding: '4px',
            display: 'flex',
            alignItems: 'center',
            gap: '4px',
            fontSize: '12px',
          }}
        >
          <RefreshCw size={13} className={loading ? 'spin' : ''} />
          <span>Refresh</span>
        </button>
      </div>

      {/* Scope & Filter Controls */}
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '8px', flexWrap: 'wrap' }}>
        {/* Moderator Scope Toggle (Mine vs Global) */}
        {canViewGlobal ? (
          <div
            style={{
              display: 'flex',
              backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
              borderRadius: '8px',
              padding: '2px',
              gap: '2px',
            }}
          >
            <button
              onClick={() => setActiveTab('mine')}
              style={{
                backgroundColor: activeTab === 'mine' ? 'var(--tg-theme-bg-color, #1c1c1e)' : 'transparent',
                color: activeTab === 'mine' ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                border: 'none',
                borderRadius: '6px',
                padding: '5px 10px',
                fontSize: '12px',
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              My Uploads
            </button>
            <button
              onClick={() => setActiveTab('global')}
              style={{
                backgroundColor: activeTab === 'global' ? 'var(--tg-theme-bg-color, #1c1c1e)' : 'transparent',
                color: activeTab === 'global' ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                border: 'none',
                borderRadius: '6px',
                padding: '5px 10px',
                fontSize: '12px',
                fontWeight: 600,
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                gap: '4px',
              }}
            >
              <Layers size={13} />
              <span>Global Queue</span>
            </button>
          </div>
        ) : (
          <div style={{ fontSize: '13px', fontWeight: 700, color: 'var(--tg-theme-text-color, #ffffff)', display: 'flex', alignItems: 'center', gap: '6px' }}>
            <Inbox size={16} />
            <span>Active Ingestion Board</span>
          </div>
        )}

        {/* Filter Toggle (Active vs History) */}
        <div
          style={{
            display: 'flex',
            backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
            borderRadius: '8px',
            padding: '2px',
            gap: '2px',
          }}
        >
          <button
            onClick={() => setFilterMode('active')}
            style={{
              backgroundColor: filterMode === 'active' ? 'var(--tg-theme-bg-color, #1c1c1e)' : 'transparent',
              color: filterMode === 'active' ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
              border: 'none',
              borderRadius: '6px',
              padding: '5px 10px',
              fontSize: '12px',
              fontWeight: 600,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
            }}
          >
            <ListFilter size={12} />
            <span>Active</span>
          </button>
          <button
            onClick={() => setFilterMode('all')}
            style={{
              backgroundColor: filterMode === 'all' ? 'var(--tg-theme-bg-color, #1c1c1e)' : 'transparent',
              color: filterMode === 'all' ? 'var(--tg-theme-button-color, #0a84ff)' : 'var(--tg-theme-subtitle-text-color, #8e8e93)',
              border: 'none',
              borderRadius: '6px',
              padding: '5px 10px',
              fontSize: '12px',
              fontWeight: 600,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
            }}
          >
            <History size={12} />
            <span>All &amp; History</span>
          </button>
        </div>
      </div>

      {/* Error state */}
      {error && (
        <div
          style={{
            backgroundColor: 'rgba(255, 69, 58, 0.1)',
            border: '1px solid rgba(255, 69, 58, 0.3)',
            borderRadius: '8px',
            padding: '8px 12px',
            fontSize: '12px',
            color: '#ff453a',
          }}
        >
          {error}
        </div>
      )}

      {/* Job List */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
        {filteredJobs.length > 0 ? (
          filteredJobs.map((job) => (
            <JobCard
              key={job.job_id}
              job={job}
              profile={profile}
              onPause={handlePause}
              onResume={handleResume}
              onCancel={handleCancel}
              onSelectCandidate={handleSelectCandidate}
              onManualSearch={handleManualSearch}
            />
          ))
        ) : (
          <div
            style={{
              textAlign: 'center',
              padding: '36px 16px',
              backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
              borderRadius: '12px',
              border: '1px solid var(--tg-theme-secondary-bg-color, #2c2c2e)',
            }}
          >
            <Inbox size={32} color="var(--tg-theme-subtitle-text-color, #636366)" style={{ margin: '0 auto 8px' }} />
            <div style={{ fontSize: '14px', fontWeight: 600, color: 'var(--tg-theme-text-color, #ffffff)' }}>
              No {filterMode === 'active' ? 'Active' : ''} Ingestion Jobs
            </div>
            <div style={{ fontSize: '12px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', marginTop: '4px' }}>
              Drop audio files or multi-track archives above to begin automatic fingerprinting and ingestion.
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

