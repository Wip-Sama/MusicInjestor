"""Role-Based Access Control (RBAC) policy matrix and user governance service."""

import logging
from datetime import UTC, datetime

from src.core.exceptions import PermissionDeniedError
from src.core.models import (
    AccessRequestRecord,
    AccessRequestStatus,
    AuditAction,
    AuditLogEntry,
    LibraryTrackRecord,
    Permission,
    TagCorrectionRequest,
    TagCorrectionStatus,
    UserAccount,
    UserRole,
)
from src.core.ports import AdminLogNotifierPort, GovernanceRepositoryPort

logger = logging.getLogger(__name__)

ROLE_PERMISSIONS: dict[UserRole, frozenset[Permission]] = {
    UserRole.GUEST: frozenset(
        {
            Permission.BASIC_COMMANDS,
            Permission.LINK_OIDC,
            Permission.SUBMIT_ACCESS_REQUEST,
        }
    ),
    UserRole.LISTENER: frozenset(
        {
            Permission.BASIC_COMMANDS,
            Permission.LINK_OIDC,
            Permission.SUBMIT_ACCESS_REQUEST,
            Permission.VIEW_CATALOG,
        }
    ),
    UserRole.UPLOADER: frozenset(
        {
            Permission.BASIC_COMMANDS,
            Permission.LINK_OIDC,
            Permission.VIEW_CATALOG,
            Permission.SUBMIT_INGESTION,
            Permission.VIEW_OWN_JOBS,
            Permission.RESOLVE_OWN_DISAMBIGUATION,
            Permission.REQUEST_TAG_CORRECTION,
        }
    ),
    UserRole.MODERATOR: frozenset(
        {
            Permission.BASIC_COMMANDS,
            Permission.LINK_OIDC,
            Permission.VIEW_CATALOG,
            Permission.SUBMIT_INGESTION,
            Permission.VIEW_OWN_JOBS,
            Permission.RESOLVE_OWN_DISAMBIGUATION,
            Permission.REQUEST_TAG_CORRECTION,
            Permission.VIEW_GLOBAL_QUEUE,
            Permission.RESOLVE_OTHERS_JOBS,
            Permission.APPROVE_TAG_CORRECTIONS,
        }
    ),
    UserRole.ADMIN: frozenset(
        {
            Permission.BASIC_COMMANDS,
            Permission.LINK_OIDC,
            Permission.VIEW_CATALOG,
            Permission.SUBMIT_INGESTION,
            Permission.VIEW_OWN_JOBS,
            Permission.RESOLVE_OWN_DISAMBIGUATION,
            Permission.REQUEST_TAG_CORRECTION,
            Permission.VIEW_GLOBAL_QUEUE,
            Permission.RESOLVE_OTHERS_JOBS,
            Permission.APPROVE_TAG_CORRECTIONS,
            Permission.MANAGE_USERS,
            Permission.REVIEW_AUDIT_LOGS,
            Permission.CHANGE_CONCURRENCY_LIMIT,
        }
    ),
    UserRole.OWNER: frozenset(
        {
            Permission.BASIC_COMMANDS,
            Permission.LINK_OIDC,
            Permission.VIEW_CATALOG,
            Permission.SUBMIT_INGESTION,
            Permission.VIEW_OWN_JOBS,
            Permission.RESOLVE_OWN_DISAMBIGUATION,
            Permission.REQUEST_TAG_CORRECTION,
            Permission.VIEW_GLOBAL_QUEUE,
            Permission.RESOLVE_OTHERS_JOBS,
            Permission.APPROVE_TAG_CORRECTIONS,
            Permission.MANAGE_USERS,
            Permission.REVIEW_AUDIT_LOGS,
            Permission.CHANGE_CONCURRENCY_LIMIT,
            Permission.SYSTEM_DESTRUCTIVE_WIPE,
        }
    ),
}


def has_permission(user: UserAccount, permission: Permission) -> bool:
    """Return True if `user` is not blocked and holds `permission` under their current role."""
    if user.is_blocked:
        return False
    allowed = ROLE_PERMISSIONS.get(user.role, frozenset())
    return permission in allowed


def require_permission(user: UserAccount, permission: Permission) -> None:
    """Raise `PermissionDeniedError` if `user` lacks `permission` or is blocked."""
    if not has_permission(user, permission):
        msg = (
            f"User {user.telegram_id} (role={user.role.value}, blocked={user.is_blocked}) "
            f"lacks required permission {permission.value}"
        )
        raise PermissionDeniedError(msg)


DEFAULT_OIDC_ROLE_MAPPING: dict[str, str] = {
    "music-admins": "ADMIN",
    "music-moderators": "MODERATOR",
    "music-uploaders": "UPLOADER",
    "music-listeners": "LISTENER",
    "music-viewers": "LISTENER",
}


def evaluate_oidc_role(
    groups: tuple[str, ...] | list[str],
    role_mapping: dict[str, str] | None = None,
) -> UserRole:
    """Map Authentik OIDC group claims to the highest matching `UserRole` (Specification Section 2.2)."""
    mapping = role_mapping if role_mapping else DEFAULT_OIDC_ROLE_MAPPING
    group_set = {g.strip() for g in groups if g.strip()}

    lookup: dict[str, UserRole] = {}
    for grp, role_name in mapping.items():
        clean_grp = grp.strip().lower()
        clean_role = role_name.strip().upper()
        if clean_grp and clean_role in UserRole.__members__:
            lookup[clean_grp] = UserRole(clean_role)

    highest_role = UserRole.GUEST
    for g in group_set:
        mapped = lookup.get(g.lower())
        if mapped is not None and mapped.rank > highest_role.rank:
            highest_role = mapped

    return highest_role


def next_promotion_role(current: UserRole) -> UserRole | None:
    """Return the next higher tier for manual admin promotion buttons."""
    promotions: dict[UserRole, UserRole] = {
        UserRole.GUEST: UserRole.LISTENER,
        UserRole.LISTENER: UserRole.UPLOADER,
        UserRole.UPLOADER: UserRole.MODERATOR,
        UserRole.MODERATOR: UserRole.ADMIN,
    }
    return promotions.get(current)


def next_demotion_role(current: UserRole) -> UserRole | None:
    """Return the next lower tier for manual admin demotion buttons."""
    demotions: dict[UserRole, UserRole] = {
        UserRole.ADMIN: UserRole.MODERATOR,
        UserRole.MODERATOR: UserRole.UPLOADER,
        UserRole.UPLOADER: UserRole.LISTENER,
        UserRole.LISTENER: UserRole.GUEST,
    }
    return demotions.get(current)


class GovernanceService:
    """Domain service orchestrating user onboarding, access requests, RBAC changes, and audit logs."""

    def __init__(
        self,
        repository: GovernanceRepositoryPort,
        notifier: AdminLogNotifierPort | None = None,
        owner_telegram_ids: tuple[int, ...] = (),
    ) -> None:
        self._repo = repository
        self._notifier = notifier
        self._owner_ids = set(owner_telegram_ids)

    async def ensure_user(
        self,
        telegram_id: int,
        display_name: str,
        username: str | None = None,
    ) -> UserAccount:
        """Load an existing user or create a new one, bootstrapping configured OWNER IDs."""
        existing = await self._repo.get_user(telegram_id)
        now = datetime.now(UTC)
        if existing is not None:
            needs_save = False
            if display_name and existing.display_name != display_name:
                existing.display_name = display_name
                needs_save = True
            if username is not None and existing.username != username:
                existing.username = username
                needs_save = True
            if telegram_id in self._owner_ids and existing.role != UserRole.OWNER and not existing.is_blocked:
                existing.role = UserRole.OWNER
                needs_save = True
            if needs_save:
                existing.updated_at = now
                await self._repo.upsert_user(existing)
            return existing

        initial_role = UserRole.OWNER if telegram_id in self._owner_ids else UserRole.GUEST
        new_user = UserAccount(
            telegram_id=telegram_id,
            display_name=display_name or f"User {telegram_id}",
            username=username,
            role=initial_role,
            created_at=now,
            updated_at=now,
        )
        return await self._repo.upsert_user(new_user)

    async def submit_access_request(
        self,
        user: UserAccount,
    ) -> tuple[AccessRequestRecord, bool]:
        """Submit a manual access request with debounce protection against button spam.

        Returns `(record, created_new)` where `created_new` is False if a PENDING request already exists.
        """
        require_permission(user, Permission.SUBMIT_ACCESS_REQUEST)
        existing_pending = await self._repo.get_pending_access_request(user.telegram_id)
        if existing_pending is not None:
            logger.debug(
                "Debounced duplicate access request from user %s (request_id=%s)",
                user.telegram_id,
                existing_pending.id,
            )
            return existing_pending, False

        request = await self._repo.create_access_request(user.telegram_id)
        await self._repo.append_audit_log(
            AuditLogEntry(
                actor_id=user.telegram_id,
                action=AuditAction.ACCESS_REQUESTED,
                target_entity=f"user:{user.telegram_id}",
                details={"request_id": request.id, "username": user.username},
            )
        )
        if self._notifier is not None:
            await self._notifier.send_access_request_card(user, request)
        return request, True

    async def approve_access_request(
        self,
        actor: UserAccount,
        request_id: int,
        granted_role: UserRole = UserRole.UPLOADER,
    ) -> UserAccount:
        """Approve a pending access request and assign `granted_role` to the requester."""
        require_permission(actor, Permission.MANAGE_USERS)
        request = await self._repo.get_access_request_by_id(request_id)
        if request is None:
            msg = f"Access request {request_id} not found"
            raise KeyError(msg)

        target_user = await self._repo.get_user(request.telegram_id)
        if target_user is None:
            msg = f"Target user {request.telegram_id} not found"
            raise KeyError(msg)

        previous_role = target_user.role
        target_user.role = granted_role
        target_user.updated_at = datetime.now(UTC)
        await self._repo.upsert_user(target_user)
        await self._repo.resolve_access_request(
            request_id=request_id,
            status=AccessRequestStatus.APPROVED,
            reviewed_by=actor.telegram_id,
        )
        await self._repo.append_audit_log(
            AuditLogEntry(
                actor_id=actor.telegram_id,
                action=AuditAction.ROLE_UPDATED,
                target_entity=f"user:{target_user.telegram_id}",
                details={
                    "previous_role": previous_role.value,
                    "new_role": granted_role.value,
                    "reason": "Manual Approval via Button",
                    "request_id": request_id,
                },
            )
        )
        if self._notifier is not None:
            await self._notifier.send_role_updated_audit(
                target_user=target_user,
                actor=actor,
                previous_role=previous_role,
                new_role=granted_role,
                reason="Manual Approval via Button",
            )
        return target_user

    async def deny_access_request(
        self,
        actor: UserAccount,
        request_id: int,
    ) -> AccessRequestRecord:
        """Reject a pending access request."""
        require_permission(actor, Permission.MANAGE_USERS)
        resolved = await self._repo.resolve_access_request(
            request_id=request_id,
            status=AccessRequestStatus.REJECTED,
            reviewed_by=actor.telegram_id,
        )
        if resolved is None:
            msg = f"Access request {request_id} not found"
            raise KeyError(msg)

        await self._repo.append_audit_log(
            AuditLogEntry(
                actor_id=actor.telegram_id,
                action=AuditAction.ACCESS_DENIED,
                target_entity=f"user:{resolved.telegram_id}",
                details={"request_id": request_id},
            )
        )
        return resolved

    async def change_user_role(
        self,
        actor: UserAccount,
        target_telegram_id: int,
        new_role: UserRole,
        reason: str = "Admin Dashboard Role Update",
        override_oidc: bool = True,
    ) -> UserAccount:
        """Promote or demote a user, cancelling queued jobs if demoted to GUEST."""
        require_permission(actor, Permission.MANAGE_USERS)
        if new_role == UserRole.OWNER:
            require_permission(actor, Permission.SYSTEM_DESTRUCTIVE_WIPE)

        target_user = await self._repo.get_user(target_telegram_id)
        if target_user is None:
            msg = f"User {target_telegram_id} not found"
            raise KeyError(msg)

        if target_user.role == UserRole.OWNER and actor.role != UserRole.OWNER:
            raise PermissionDeniedError("Only an OWNER can modify another OWNER account.")

        previous_role = target_user.role
        target_user.role = new_role
        if target_user.is_oidc_linked and override_oidc:
            target_user.oidc_role_override = True
        target_user.updated_at = datetime.now(UTC)
        await self._repo.upsert_user(target_user)

        cancelled_count = 0
        if new_role == UserRole.GUEST:
            cancelled_count = await self._repo.cancel_queued_jobs_for_user(target_telegram_id)

        pending_req = await self._repo.get_pending_access_request(target_telegram_id)
        if pending_req is not None and pending_req.id is not None:
            resolution_status = (
                AccessRequestStatus.REJECTED if new_role == UserRole.GUEST else AccessRequestStatus.APPROVED
            )
            await self._repo.resolve_access_request(
                request_id=pending_req.id,
                status=resolution_status,
                reviewed_by=actor.telegram_id,
            )

        await self._repo.append_audit_log(
            AuditLogEntry(
                actor_id=actor.telegram_id,
                action=AuditAction.ROLE_UPDATED,
                target_entity=f"user:{target_user.telegram_id}",
                details={
                    "previous_role": previous_role.value,
                    "new_role": new_role.value,
                    "reason": reason,
                    "cancelled_jobs": cancelled_count,
                    "oidc_role_override": target_user.oidc_role_override,
                },
            )
        )
        if self._notifier is not None:
            await self._notifier.send_role_updated_audit(
                target_user=target_user,
                actor=actor,
                previous_role=previous_role,
                new_role=new_role,
                reason=reason,
            )
        return target_user

    async def set_user_oidc_override(
        self,
        actor: UserAccount,
        target_telegram_id: int,
        override: bool,
    ) -> UserAccount:
        """Toggle local RBAC override over Authentik OIDC group synchronization."""
        require_permission(actor, Permission.MANAGE_USERS)
        target_user = await self._repo.get_user(target_telegram_id)
        if target_user is None:
            msg = f"User {target_telegram_id} not found"
            raise KeyError(msg)

        if target_user.role == UserRole.OWNER and actor.role != UserRole.OWNER:
            raise PermissionDeniedError("Only an OWNER can modify another OWNER account.")

        target_user.oidc_role_override = override
        target_user.updated_at = datetime.now(UTC)
        await self._repo.upsert_user(target_user)

        await self._repo.append_audit_log(
            AuditLogEntry(
                actor_id=actor.telegram_id,
                action=AuditAction.OIDC_OVERRIDE_UPDATED,
                target_entity=f"user:{target_user.telegram_id}",
                details={
                    "oidc_role_override": override,
                    "current_role": target_user.role.value,
                },
            )
        )
        return target_user

    async def unlink_user_oidc(
        self,
        actor: UserAccount,
        target_telegram_id: int,
    ) -> UserAccount:
        """Disconnect/unlink an Authentik OIDC identity for a user.

        Can be executed by the user themselves or by an administrator with MANAGE_USERS permission.
        """
        if actor.telegram_id != target_telegram_id:
            require_permission(actor, Permission.MANAGE_USERS)

        target_user = await self._repo.get_user(target_telegram_id)
        if target_user is None:
            msg = f"User {target_telegram_id} not found"
            raise KeyError(msg)

        old_sub = target_user.authentik_sub
        old_username = target_user.authentik_username
        target_user.authentik_sub = None
        target_user.authentik_username = None
        target_user.authentik_groups = ()
        target_user.access_token_encrypted = None
        target_user.refresh_token_encrypted = None
        target_user.token_expires_at = None
        target_user.last_oidc_sync = None
        target_user.oidc_role_override = False
        target_user.updated_at = datetime.now(UTC)
        await self._repo.upsert_user(target_user)

        await self._repo.append_audit_log(
            AuditLogEntry(
                actor_id=actor.telegram_id,
                action=AuditAction.OIDC_UNLINKED,
                target_entity=f"user:{target_user.telegram_id}",
                details={
                    "previous_authentik_sub": old_sub,
                    "previous_authentik_username": old_username,
                    "unlinked_by": actor.telegram_id,
                },
            )
        )
        return target_user

    async def set_user_blocked(
        self,
        actor: UserAccount,
        target_telegram_id: int,
        is_blocked: bool,
        reason: str = "Admin Block/Ban Action",
    ) -> UserAccount:
        """Block/ban or unblock a user, revoking active queued jobs when blocked."""
        require_permission(actor, Permission.MANAGE_USERS)
        target_user = await self._repo.get_user(target_telegram_id)
        if target_user is None:
            msg = f"User {target_telegram_id} not found"
            raise KeyError(msg)

        if target_user.role == UserRole.OWNER:
            raise PermissionDeniedError("Cannot block an OWNER account.")

        previous_role = target_user.role
        target_user.is_blocked = is_blocked
        cancelled_count = 0
        if is_blocked:
            target_user.role = UserRole.GUEST
            cancelled_count = await self._repo.cancel_queued_jobs_for_user(target_telegram_id)
            pending_req = await self._repo.get_pending_access_request(target_telegram_id)
            if pending_req is not None and pending_req.id is not None:
                await self._repo.resolve_access_request(
                    request_id=pending_req.id,
                    status=AccessRequestStatus.REJECTED,
                    reviewed_by=actor.telegram_id,
                )
        target_user.updated_at = datetime.now(UTC)
        await self._repo.upsert_user(target_user)

        action = AuditAction.USER_BLOCKED if is_blocked else AuditAction.USER_UNBLOCKED
        await self._repo.append_audit_log(
            AuditLogEntry(
                actor_id=actor.telegram_id,
                action=action,
                target_entity=f"user:{target_user.telegram_id}",
                details={
                    "is_blocked": is_blocked,
                    "previous_role": previous_role.value,
                    "new_role": target_user.role.value,
                    "cancelled_jobs": cancelled_count,
                    "reason": reason,
                },
            )
        )
        return target_user

    async def request_tag_correction(
        self,
        actor: UserAccount,
        track_id: str,
        proposed_title: str | None = None,
        proposed_artist: str | None = None,
        proposed_album: str | None = None,
        proposed_year: int | None = None,
        reason: str = "",
    ) -> tuple[TagCorrectionRequest, LibraryTrackRecord]:
        """Submit a post-ingestion tag correction request (`REQUEST_TAG_CORRECTION`)."""
        require_permission(actor, Permission.REQUEST_TAG_CORRECTION)
        track = await self._repo.get_library_track(track_id)
        if track is None:
            msg = f"Library track '{track_id}' not found"
            raise KeyError(msg)

        request = await self._repo.create_tag_correction_request(
            TagCorrectionRequest(
                track_id=track.id,
                requested_by=actor.telegram_id,
                proposed_title=proposed_title,
                proposed_artist=proposed_artist,
                proposed_album=proposed_album,
                proposed_year=proposed_year,
                reason=reason,
            )
        )
        await self._repo.append_audit_log(
            AuditLogEntry(
                actor_id=actor.telegram_id,
                action=AuditAction.TAG_CORRECTION_REQUESTED,
                target_entity=f"track:{track.id}",
                details={
                    "request_id": request.id,
                    "proposed_title": proposed_title,
                    "proposed_artist": proposed_artist,
                    "proposed_album": proposed_album,
                    "proposed_year": proposed_year,
                    "reason": reason,
                },
            )
        )
        return request, track

    async def approve_tag_correction(
        self,
        actor: UserAccount,
        request_id: int,
    ) -> tuple[TagCorrectionRequest, LibraryTrackRecord]:
        """Approve a pending tag correction request and update track metadata (`APPROVE_TAG_CORRECTIONS`)."""
        require_permission(actor, Permission.APPROVE_TAG_CORRECTIONS)
        request = await self._repo.get_tag_correction_request(request_id)
        if request is None:
            msg = f"Tag correction request {request_id} not found"
            raise KeyError(msg)

        updated_track = await self._repo.update_library_track_metadata(
            track_id=request.track_id,
            title=request.proposed_title,
            artist=request.proposed_artist,
            album=request.proposed_album,
            year=request.proposed_year,
            modified_by=actor.telegram_id,
        )
        if updated_track is None:
            msg = f"Library track '{request.track_id}' not found"
            raise KeyError(msg)

        resolved = await self._repo.resolve_tag_correction_request(
            request_id=request_id,
            status=TagCorrectionStatus.APPROVED,
            reviewed_by=actor.telegram_id,
        )
        assert resolved is not None

        await self._repo.append_audit_log(
            AuditLogEntry(
                actor_id=actor.telegram_id,
                action=AuditAction.TAG_CORRECTION_APPROVED,
                target_entity=f"track:{updated_track.id}",
                details={
                    "request_id": request_id,
                    "title": updated_track.title,
                    "artist": updated_track.artist,
                    "album": updated_track.album,
                    "year": updated_track.year,
                },
            )
        )
        return resolved, updated_track

    async def reject_tag_correction(
        self,
        actor: UserAccount,
        request_id: int,
    ) -> TagCorrectionRequest:
        """Reject a pending tag correction request (`APPROVE_TAG_CORRECTIONS`)."""
        require_permission(actor, Permission.APPROVE_TAG_CORRECTIONS)
        resolved = await self._repo.resolve_tag_correction_request(
            request_id=request_id,
            status=TagCorrectionStatus.REJECTED,
            reviewed_by=actor.telegram_id,
        )
        if resolved is None:
            msg = f"Tag correction request {request_id} not found"
            raise KeyError(msg)

        await self._repo.append_audit_log(
            AuditLogEntry(
                actor_id=actor.telegram_id,
                action=AuditAction.TAG_CORRECTION_REJECTED,
                target_entity=f"track:{resolved.track_id}",
                details={"request_id": request_id},
            )
        )
        return resolved

    async def wipe_library_catalog(
        self,
        actor: UserAccount,
        reason: str = "Owner Destructive Library Wipe",
    ) -> int:
        """Execute a destructive library catalog wipe (`SYSTEM_DESTRUCTIVE_WIPE`)."""
        require_permission(actor, Permission.SYSTEM_DESTRUCTIVE_WIPE)
        removed_count = await self._repo.wipe_library_catalog()
        await self._repo.append_audit_log(
            AuditLogEntry(
                actor_id=actor.telegram_id,
                action=AuditAction.SYSTEM_WIPE_EXECUTED,
                target_entity="system:catalog",
                details={"removed_records": removed_count, "reason": reason},
            )
        )
        return removed_count
