export interface AdminUser {
  telegram_id: number;
  role: 'GUEST' | 'LISTENER' | 'UPLOADER' | 'MODERATOR' | 'ADMIN' | 'OWNER';
  display_name: string;
  username: string | null;
  is_blocked: boolean;
  authentik_username: string | null;
  created_at: string | null;
  updated_at: string | null;
  uploads_count: {
    albums: number;
    tracks: number;
  };
}

export interface PendingAccessRequest {
  id: number;
  telegram_id: number;
  display_name: string;
  username: string | null;
  status: 'PENDING' | 'APPROVED' | 'REJECTED';
  created_at: string | null;
}

export interface AdminUsersResponse {
  users: AdminUser[];
}

export interface PendingAccessRequestsResponse {
  requests: PendingAccessRequest[];
}

