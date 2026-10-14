import { apiFetch, ensureInitData, getRawInitData } from './auth';
import { QueueEvent, QueueJob, QueueResponse } from '../types/queue';

export async function fetchQueue(mineOnly: boolean = false): Promise<QueueResponse> {
  const query = mineOnly ? '?mine=true' : '';
  const resp = await apiFetch(`/api/v1/app/queue${query}`);
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ error: 'Failed to fetch queue' }));
    throw new Error(err.error || `HTTP ${resp.status}`);
  }
  return resp.json();
}

export async function uploadMediaFile(
  file: File,
  onProgress?: (percent: number) => void
): Promise<{ success: boolean; job: QueueJob }> {
  const initData = await ensureInitData();
  const formData = new FormData();
  formData.append('file', file, file.name);

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/v1/app/ingest/upload', true);
    if (initData) {
      xhr.setRequestHeader('Authorization', `tma ${initData}`);
      xhr.setRequestHeader('X-Telegram-Init-Data', initData);
    }

    if (xhr.upload && onProgress) {
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) {
          const pct = Math.round((event.loaded / event.total) * 100);
          onProgress(pct);
        }
      };
    }

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const data = JSON.parse(xhr.responseText);
          resolve(data);
        } catch {
          reject(new Error('Invalid response from server.'));
        }
      } else {
        try {
          const err = JSON.parse(xhr.responseText);
          reject(new Error(err.error || `Upload failed with HTTP ${xhr.status}`));
        } catch {
          reject(new Error(`Upload failed with HTTP ${xhr.status}`));
        }
      }
    };

    xhr.onerror = () => {
      reject(new Error('Network error during file upload.'));
    };

    xhr.send(formData);
  });
}

export async function executeQueueAction(
  jobId: string,
  payload: Record<string, unknown>
): Promise<QueueJob> {
  const resp = await apiFetch(`/api/v1/app/queue/${encodeURIComponent(jobId)}/action`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ error: 'Action failed' }));
    throw new Error(err.error || `HTTP ${resp.status}`);
  }
  const data = await resp.json();
  return data.job;
}

export async function pauseJob(jobId: string): Promise<QueueJob> {
  return executeQueueAction(jobId, { action: 'pause' });
}

export async function resumeJob(jobId: string): Promise<QueueJob> {
  return executeQueueAction(jobId, { action: 'resume' });
}

export async function cancelJob(jobId: string): Promise<QueueJob> {
  return executeQueueAction(jobId, { action: 'cancel' });
}

export async function selectCandidate(jobId: string, candidateIndex: number): Promise<QueueJob> {
  return executeQueueAction(jobId, { action: 'select_candidate', candidate_index: candidateIndex });
}

export async function manualSearch(jobId: string, query: string): Promise<QueueJob> {
  return executeQueueAction(jobId, { action: 'manual_search', query });
}

export function connectQueueEvents(
  onEvent: (event: QueueEvent) => void,
  onError?: (err: Event) => void
): () => void {
  let source: EventSource | null = null;
  let closed = false;

  const init = async () => {
    let initData = getRawInitData();
    if (!initData) {
      initData = await ensureInitData();
    }
    if (closed) return;

    const url = `/api/v1/app/queue/events?tgWebAppData=${encodeURIComponent(initData)}`;
    source = new EventSource(url);

    const eventTypes: Array<QueueEvent['type']> = [
      'connected',
      'job_enqueued',
      'stage_changed',
      'confirmation_required',
      'job_completed',
      'job_failed',
      'job_state_changed',
    ];

    eventTypes.forEach((type) => {
      source?.addEventListener(type, (e: MessageEvent) => {
        try {
          const parsed = JSON.parse(e.data);
          onEvent({ type, ...parsed });
        } catch {
          // Ignored
        }
      });
    });

    source.onerror = (e) => {
      if (onError) onError(e);
    };
  };

  init();

  return () => {
    closed = true;
    if (source) {
      source.close();
      source = null;
    }
  };
}

