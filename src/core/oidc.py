"""Authentik OIDC deep-link pairing, HMAC state nonces, token encryption, and dynamic claim sync."""

import base64
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from src.core.exceptions import OidcAuthenticationError
from src.core.models import AuditAction, AuditLogEntry, UserAccount, UserRole
from src.core.ports import AdminLogNotifierPort, GovernanceRepositoryPort
from src.core.rbac import evaluate_oidc_role

logger = logging.getLogger(__name__)

NONCE_TTL_SECONDS = 600  # 10-minute TTL (Specification Section 2.1)
STALE_SYNC_SECONDS = 900  # 15-minute staleness threshold (Specification Section 2.2)


def generate_state_nonce(
    telegram_id: int,
    secret_key: str,
    now_ts: int | None = None,
) -> str:
    """Generate a cryptographic state nonce binding `telegram_id` and `timestamp` via HMAC-SHA256."""
    timestamp = now_ts if now_ts is not None else int(time.time())
    payload = f"{telegram_id}:{timestamp}"
    signature = hmac.new(
        secret_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{telegram_id}.{timestamp}.{signature}"


def verify_state_nonce(
    nonce: str,
    secret_key: str,
    ttl_seconds: int = NONCE_TTL_SECONDS,
    now_ts: int | None = None,
) -> int:
    """Validate an OIDC state nonce signature and 10-minute TTL, returning `telegram_id`."""
    parts = nonce.split(".")
    if len(parts) != 3:
        raise OidcAuthenticationError("Malformed OIDC state nonce.")

    raw_tid, raw_ts, provided_sig = parts
    try:
        telegram_id = int(raw_tid)
        timestamp = int(raw_ts)
    except ValueError as exc:
        raise OidcAuthenticationError("Invalid numeric fields in OIDC state nonce.") from exc

    expected_payload = f"{telegram_id}:{timestamp}"
    expected_sig = hmac.new(
        secret_key.encode("utf-8"),
        expected_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(provided_sig, expected_sig):
        raise OidcAuthenticationError("OIDC state nonce signature mismatch.")

    current_ts = now_ts if now_ts is not None else int(time.time())
    if current_ts - timestamp > ttl_seconds or timestamp > current_ts + 60:
        raise OidcAuthenticationError("OIDC state nonce has expired.")

    return telegram_id


def _derive_keystream(secret_key: str, salt: bytes, length: int) -> bytes:
    """Derive a deterministic pseudorandom keystream of `length` bytes using SHA-256 blocks."""
    blocks: list[bytes] = []
    counter = 0
    while sum(len(b) for b in blocks) < length:
        block = hmac.new(
            secret_key.encode("utf-8"),
            salt + counter.to_bytes(4, "big"),
            hashlib.sha256,
        ).digest()
        blocks.append(block)
        counter += 1
    return b"".join(blocks)[:length]


def encrypt_token(plaintext: str, secret_key: str) -> str:
    """Encrypt an OAuth2 token with a random salt and HMAC-SHA256 authentication tag."""
    salt = secrets.token_bytes(16)
    raw = plaintext.encode("utf-8")
    stream = _derive_keystream(secret_key, salt, len(raw))
    ciphertext = bytes(a ^ b for a, b in zip(raw, stream, strict=True))
    mac = hmac.new(secret_key.encode("utf-8"), salt + ciphertext, hashlib.sha256).digest()
    packed = salt + mac + ciphertext
    return base64.urlsafe_b64encode(packed).decode("ascii")


def decrypt_token(token_encrypted: str, secret_key: str) -> str:
    """Verify integrity and decrypt an encrypted OAuth2 token."""
    try:
        packed = base64.urlsafe_b64decode(token_encrypted.encode("ascii"))
    except Exception as exc:
        raise OidcAuthenticationError("Corrupted encrypted token encoding.") from exc

    if len(packed) < 48:
        raise OidcAuthenticationError("Encrypted token payload is truncated.")

    salt = packed[:16]
    mac = packed[16:48]
    ciphertext = packed[48:]
    expected_mac = hmac.new(
        secret_key.encode("utf-8"),
        salt + ciphertext,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(mac, expected_mac):
        raise OidcAuthenticationError("Encrypted token authentication tag mismatch.")

    stream = _derive_keystream(secret_key, salt, len(ciphertext))
    raw = bytes(a ^ b for a, b in zip(ciphertext, stream, strict=True))
    return raw.decode("utf-8")


def _describe_group_diff(old_groups: tuple[str, ...], new_groups: tuple[str, ...]) -> str:
    """Summarize added/removed Authentik groups for Admin Log Channel Event B."""
    old_set = set(old_groups)
    new_set = set(new_groups)
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)
    parts: list[str] = []
    if added:
        parts.append("Added to " + ", ".join(f"'{g}'" for g in added))
    if removed:
        parts.append("Removed from " + ", ".join(f"'{g}'" for g in removed))
    return "; ".join(parts) if parts else "No group changes"


@dataclass(frozen=True, slots=True)
class OidcSyncOutcome:
    """Result of an initial OIDC pairing or dynamic claim synchronization."""

    user: UserAccount
    previous_role: UserRole
    new_role: UserRole
    role_changed: bool
    was_blocked: bool
    cancelled_jobs: int
    group_changes_description: str


class AuthentikOidcService:
    """Domain service managing Authentik OIDC code exchange, token refresh, and role sync."""

    def __init__(
        self,
        repository: GovernanceRepositoryPort,
        secret_key: str,
        authorize_url: str,
        token_url: str,
        userinfo_url: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        notifier: AdminLogNotifierPort | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        role_mapping: dict[str, str] | None = None,
    ) -> None:
        self._repo = repository
        self._secret_key = secret_key
        self._authorize_url = authorize_url.rstrip("/")
        self._token_url = token_url.rstrip("/")
        self._userinfo_url = userinfo_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._notifier = notifier
        self._transport = transport
        self._role_mapping = role_mapping

    def build_authorization_url(
        self,
        telegram_id: int,
        now_ts: int | None = None,
    ) -> tuple[str, str]:
        """Build the Authentik OAuth2 authorization URL and state nonce (Section 2.1)."""
        nonce = generate_state_nonce(telegram_id, self._secret_key, now_ts=now_ts)
        query = urlencode(
            {
                "client_id": self._client_id,
                "response_type": "code",
                "scope": "openid profile email groups",
                "redirect_uri": self._redirect_uri,
                "state": nonce,
            }
        )
        return f"{self._authorize_url}/?{query}", nonce

    async def exchange_code_and_pair(
        self,
        code: str,
        state_nonce: str,
        now_ts: int | None = None,
    ) -> OidcSyncOutcome:
        """Validate `state_nonce`, exchange `code` for tokens, fetch `/userinfo`, and pair user."""
        telegram_id = verify_state_nonce(
            state_nonce,
            self._secret_key,
            now_ts=now_ts,
        )
        user = await self._repo.get_user(telegram_id)
        if user is None:
            user = UserAccount(
                telegram_id=telegram_id,
                display_name=f"User {telegram_id}",
            )

        async with httpx.AsyncClient(transport=self._transport, timeout=15.0) as client:
            token_resp = await client.post(
                f"{self._token_url}/",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._redirect_uri,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            if token_resp.status_code != 200:
                msg = f"Authentik token exchange failed with HTTP {token_resp.status_code}"
                raise OidcAuthenticationError(msg)

            token_data = token_resp.json()
            access_token = str(token_data.get("access_token", ""))
            refresh_token = str(token_data.get("refresh_token", ""))
            expires_in = int(token_data.get("expires_in", 3600))

            if not access_token:
                raise OidcAuthenticationError("Authentik response missing access_token.")

            userinfo_resp = await client.get(
                f"{self._userinfo_url}/",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if userinfo_resp.status_code != 200:
                msg = f"Authentik userinfo request failed with HTTP {userinfo_resp.status_code}"
                raise OidcAuthenticationError(msg)

            claims: dict[str, object] = userinfo_resp.json()

        now = datetime.now(UTC)
        user.access_token_encrypted = encrypt_token(access_token, self._secret_key)
        if refresh_token:
            user.refresh_token_encrypted = encrypt_token(refresh_token, self._secret_key)
        user.token_expires_at = now + timedelta(seconds=expires_in)

        return await self.sync_user_claims(user, claims)

    async def sync_user_claims(
        self,
        user: UserAccount,
        claims: dict[str, object],
    ) -> OidcSyncOutcome:
        """Apply Authentik `/userinfo` or webhook claims to `user` and enforce role/deactivation rules."""
        previous_role = user.role
        old_groups = user.authentik_groups

        sub = str(claims.get("sub") or user.authentik_sub or "")
        if not sub:
            raise OidcAuthenticationError("Authentik claims missing 'sub' identifier.")

        preferred_username = str(
            claims.get("preferred_username")
            or claims.get("username")
            or claims.get("email")
            or user.authentik_username
            or ""
        )
        raw_groups = claims.get("groups") or claims.get("roles") or []
        if isinstance(raw_groups, (list, tuple)):
            new_groups = tuple(str(g) for g in raw_groups)
        elif isinstance(raw_groups, str):
            new_groups = tuple(g.strip() for g in raw_groups.split(",") if g.strip())
        else:
            new_groups = ()
        is_active = bool(claims.get("is_active", claims.get("active", True)))

        now = datetime.now(UTC)
        user.authentik_sub = sub
        if preferred_username:
            user.authentik_username = preferred_username
        user.authentik_groups = new_groups
        user.last_oidc_sync = now
        user.updated_at = now

        cancelled_jobs = 0
        if not is_active:
            user.is_blocked = True
            user.role = UserRole.GUEST
            cancelled_jobs = await self._repo.cancel_queued_jobs_for_user(user.telegram_id)
            group_diff = "Account deactivated/disabled in Authentik"
        else:
            mapped_role = evaluate_oidc_role(new_groups, self._role_mapping)
            if user.role != UserRole.OWNER:
                if user.oidc_role_override:
                    logger.info(
                        "User %d has oidc_role_override=True; retaining local role %s instead of IDP role %s",
                        user.telegram_id,
                        user.role.value,
                        mapped_role.value,
                    )
                else:
                    user.role = mapped_role
            if user.role == UserRole.GUEST and previous_role != UserRole.GUEST:
                cancelled_jobs = await self._repo.cancel_queued_jobs_for_user(user.telegram_id)
            group_diff = _describe_group_diff(old_groups, new_groups)

        await self._repo.upsert_user(user)
        role_changed = user.role != previous_role or not is_active

        if role_changed:
            await self._repo.append_audit_log(
                AuditLogEntry(
                    actor_id=user.telegram_id,
                    action=AuditAction.OIDC_SYNC,
                    target_entity=f"user:{user.telegram_id}",
                    details={
                        "authentik_sub": sub,
                        "previous_role": previous_role.value,
                        "new_role": user.role.value,
                        "group_changes": group_diff,
                        "cancelled_jobs": cancelled_jobs,
                        "is_blocked": user.is_blocked,
                    },
                )
            )
            if self._notifier is not None:
                await self._notifier.send_oidc_sync_audit(
                    target_user=user,
                    group_changes=group_diff,
                    new_role=user.role,
                    cancelled_jobs_count=cancelled_jobs,
                )

        return OidcSyncOutcome(
            user=user,
            previous_role=previous_role,
            new_role=user.role,
            role_changed=role_changed,
            was_blocked=user.is_blocked,
            cancelled_jobs=cancelled_jobs,
            group_changes_description=group_diff,
        )

    async def refresh_user_from_idp(self, user: UserAccount) -> OidcSyncOutcome:
        """Query Authentik `/userinfo` (refreshing access token if expired) and synchronize claims."""
        if not user.is_oidc_linked:
            msg = f"User {user.telegram_id} is not linked to Authentik OIDC."
            raise OidcAuthenticationError(msg)

        now = datetime.now(UTC)
        access_token: str | None = None
        if user.access_token_encrypted:
            access_token = decrypt_token(user.access_token_encrypted, self._secret_key)

        token_expired = user.token_expires_at is not None and user.token_expires_at <= now
        async with httpx.AsyncClient(transport=self._transport, timeout=15.0) as client:
            if (not access_token or token_expired) and user.refresh_token_encrypted:
                refresh_tok = decrypt_token(user.refresh_token_encrypted, self._secret_key)
                refresh_resp = await client.post(
                    f"{self._token_url}/",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_tok,
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                    },
                )
                if refresh_resp.status_code != 200:
                    return await self.sync_user_claims(
                        user,
                        {"sub": user.authentik_sub, "is_active": False, "groups": []},
                    )
                token_payload = refresh_resp.json()
                access_token = str(token_payload.get("access_token", ""))
                new_refresh = str(token_payload.get("refresh_token", ""))
                expires_in = int(token_payload.get("expires_in", 3600))
                user.access_token_encrypted = encrypt_token(access_token, self._secret_key)
                if new_refresh:
                    user.refresh_token_encrypted = encrypt_token(new_refresh, self._secret_key)
                user.token_expires_at = now + timedelta(seconds=expires_in)

            if not access_token:
                raise OidcAuthenticationError("No valid access token available for OIDC refresh.")

            userinfo_resp = await client.get(
                f"{self._userinfo_url}/",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if userinfo_resp.status_code in {401, 403, 404}:
                return await self.sync_user_claims(
                    user,
                    {"sub": user.authentik_sub, "is_active": False, "groups": []},
                )
            if userinfo_resp.status_code != 200:
                msg = f"Unexpected userinfo status {userinfo_resp.status_code}"
                raise OidcAuthenticationError(msg)

            claims: dict[str, object] = userinfo_resp.json()

        return await self.sync_user_claims(user, claims)

    async def refresh_user_if_stale(
        self,
        telegram_id: int,
        max_age_seconds: int = STALE_SYNC_SECONDS,
    ) -> UserAccount:
        """Refresh an Authentik-linked user's profile if `last_oidc_sync` is older than 15 minutes."""
        user = await self._repo.get_user(telegram_id)
        if user is None:
            msg = f"Unknown user {telegram_id}"
            raise KeyError(msg)
        if not user.is_oidc_linked:
            return user

        now = datetime.now(UTC)
        last_sync = user.last_oidc_sync
        if last_sync is None or (now - last_sync.astimezone(UTC)).total_seconds() > max_age_seconds:
            outcome = await self.refresh_user_from_idp(user)
            return outcome.user
        return user

    async def sync_all_linked_users(self) -> list[OidcSyncOutcome]:
        """Periodic re-check of all active Authentik-linked users (Specification Section 2.2)."""
        linked_users = await self._repo.list_oidc_linked_users()
        outcomes: list[OidcSyncOutcome] = []
        for user in linked_users:
            try:
                outcome = await self.refresh_user_from_idp(user)
                outcomes.append(outcome)
            except Exception as exc:
                logger.warning("Failed OIDC sync for user %s: %s", user.telegram_id, exc)
        return outcomes

    async def handle_webhook_event(
        self,
        event_type: str,
        authentik_sub: str,
        claims: dict[str, object] | None = None,
    ) -> OidcSyncOutcome | None:
        """Process an incoming Authentik webhook (`user_updated`, `user_deactivated`, `user_deleted`)."""
        user = await self._repo.get_user_by_authentik_sub(authentik_sub)
        if user is None:
            logger.debug("Ignoring webhook for unknown Authentik sub=%s", authentik_sub)
            return None

        normalized_event = event_type.strip().lower()
        if normalized_event in {"user_deactivated", "user_deleted", "user_disabled"}:
            return await self.sync_user_claims(
                user,
                {"sub": authentik_sub, "is_active": False, "groups": []},
            )

        if claims is not None:
            merged_claims = dict(claims)
            merged_claims.setdefault("sub", authentik_sub)
            return await self.sync_user_claims(user, merged_claims)

        return await self.refresh_user_from_idp(user)

    async def unlink_user_oidc(self, user: UserAccount, actor_id: int | None = None) -> UserAccount:
        """Disconnect and unlink an Authentik OIDC identity from `user`."""
        old_sub = user.authentik_sub
        old_username = user.authentik_username
        user.authentik_sub = None
        user.authentik_username = None
        user.authentik_groups = ()
        user.access_token_encrypted = None
        user.refresh_token_encrypted = None
        user.token_expires_at = None
        user.last_oidc_sync = None
        user.oidc_role_override = False
        user.updated_at = datetime.now(UTC)
        await self._repo.upsert_user(user)

        effective_actor = actor_id or user.telegram_id
        await self._repo.append_audit_log(
            AuditLogEntry(
                actor_id=effective_actor,
                action=AuditAction.OIDC_UNLINKED,
                target_entity=f"user:{user.telegram_id}",
                details={
                    "previous_authentik_sub": old_sub,
                    "previous_authentik_username": old_username,
                    "unlinked_by": effective_actor,
                },
            )
        )
        return user
