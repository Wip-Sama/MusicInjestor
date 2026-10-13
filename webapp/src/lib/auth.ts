/**
 * Resilient Telegram Mini App `initData` capture and authenticated API client.
 * Adheres strictly to Section 3.3 & 4.1 of the Telegram Mini Apps specification.
 */

import { retrieveLaunchParams } from '@telegram-apps/sdk-react';

export interface UserProfile {
  telegram_id: number;
  role: 'GUEST' | 'UPLOADER' | 'MODERATOR' | 'ADMIN' | 'OWNER';
  display_name: string;
  username: string | null;
  authentik_linked: boolean;
  authentik_username: string | null;
  permissions: string[];
  is_owner: boolean;
  start_param: string;
}

const STORAGE_KEY = 'tg_init_data';

/**
 * Extract raw initData via cascading lookup:
 * 1. window.Telegram.WebApp.initData (live injection from Telegram container)
 * 2. retrieveLaunchParams() from @telegram-apps/sdk-react
 * 3. URL hash (#tgWebAppData=...) or search query (?tgWebAppData=...)
 * 4. sessionStorage cache (fallback across client route navigation)
 */
export function getRawInitData(): string {
  if (typeof window === 'undefined') return '';

  // 1. Direct Telegram WebApp global injection (always fresh when opened inside Telegram)
  const tgGlobal = (window as unknown as { Telegram?: { WebApp?: { initData?: string } } })?.Telegram?.WebApp;
  if (tgGlobal?.initData && tgGlobal.initData.trim()) {
    sessionStorage.setItem(STORAGE_KEY, tgGlobal.initData.trim());
    return tgGlobal.initData.trim();
  }

  // 2. Official Telegram Apps SDK launch parameters
  try {
    const lp = retrieveLaunchParams();
    if (lp.initDataRaw && lp.initDataRaw.trim()) {
      sessionStorage.setItem(STORAGE_KEY, lp.initDataRaw.trim());
      return lp.initDataRaw.trim();
    }
  } catch {
    // SDK launch params not available in current environment
  }

  // 3. Fallback: URL hash
  if (window.location.hash) {
    const hash = window.location.hash.substring(1);
    const params = new URLSearchParams(hash);
    const dataFromHash = params.get('tgWebAppData');
    if (dataFromHash && dataFromHash.trim()) {
      sessionStorage.setItem(STORAGE_KEY, dataFromHash.trim());
      return dataFromHash.trim();
    }
  }

  // 4. Fallback: URL query parameters
  const searchParams = new URLSearchParams(window.location.search);
  const dataFromSearch = searchParams.get('tgWebAppData');
  if (dataFromSearch && dataFromSearch.trim()) {
    sessionStorage.setItem(STORAGE_KEY, dataFromSearch.trim());
    return dataFromSearch.trim();
  }

  // 5. Fallback: Session storage
  const cached = sessionStorage.getItem(STORAGE_KEY);
  if (cached && cached.trim()) return cached.trim();

  return '';
}

/**
 * Ensure valid initData exists.
 * When accessed outside Telegram in local development (localhost / 127.0.0.1),
 * requests a freshly signed development token from `/api/v1/app/dev/token`.
 */
export async function ensureInitData(): Promise<string> {
  let initData = getRawInitData();
  if (!initData && typeof window !== 'undefined') {
    const isLocal =
      window.location.hostname === 'localhost' ||
      window.location.hostname === '127.0.0.1';
    if (isLocal) {
      try {
        const resp = await fetch('/api/v1/app/dev/token');
        if (resp.ok) {
          const body = await resp.json();
          if (body.init_data) {
            sessionStorage.setItem(STORAGE_KEY, body.init_data);
            initData = body.init_data;
          }
        }
      } catch {
        // Dev server not reachable or endpoint disabled
      }
    }
  }
  return initData;
}

/**
 * Authenticated HTTP wrapper injecting `Authorization: tma <rawInitData>`
 * and `X-Telegram-Init-Data`.
 */
export async function apiFetch(input: string | URL, init: RequestInit = {}): Promise<Response> {
  let initData = getRawInitData();
  if (!initData) {
    initData = await ensureInitData();
  }
  const headers = new Headers(init.headers || {});

  if (initData) {
    headers.set('Authorization', `tma ${initData}`);
    headers.set('X-Telegram-Init-Data', initData);
  }

  return fetch(input, {
    ...init,
    headers,
  });
}

/**
 * Fetch the authenticated user's profile and permissions.
 */
export async function fetchCurrentProfile(): Promise<UserProfile> {
  const resp = await apiFetch('/api/v1/app/auth/me');
  if (!resp.ok) {
    const errData = await resp.json().catch(() => ({}));
    throw new Error(errData.error || `Failed to authenticate (HTTP ${resp.status})`);
  }
  return resp.json();
}

