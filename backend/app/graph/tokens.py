"""Delegated access tokens for Microsoft Graph.

Graph is only used to read source calendars (customer tenants, delegated Calendars.Read),
and there are no app-only tokens. Each linked source stores the refresh token its owner
granted when they signed in. Access tokens are redeemed from it at the source's own
tenant endpoint (never `common`), cached per source in Redis, and refreshed under a
per-source lock so concurrent workers never race on a rotated token.
"""

import base64
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from redis.asyncio import Redis
from redis.exceptions import LockError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import LinkedSource, Tenant

log = logging.getLogger(__name__)

# What a source's refresh token is redeemed for. offline_access keeps rotation going.
SOURCE_REFRESH_SCOPE = "https://graph.microsoft.com/Calendars.Read offline_access"
REFRESH_MARGIN_SECONDS = 300
REFRESH_LOCK_SECONDS = 30

# OAuth `error` values on a token response that mean only the user can fix it, by
# signing in again (RFC 6749 invalid_grant, plus the OIDC interaction errors).
REAUTH_ERRORS = {"invalid_grant", "interaction_required", "consent_required", "login_required"}

# AADSTS codes that mean the delegated grant no longer works and the user has to sign
# in again. They are classified as reauth even when `error` is something else.
REAUTH_CODES = {
    "AADSTS50076",  # MFA required (Conditional Access / per-user MFA)
    "AADSTS50078",  # presented MFA claim expired
    "AADSTS50079",  # user must register for MFA
    "AADSTS50158",  # external security challenge not satisfied
    "AADSTS50173",  # grant expired after a password change or reset
    "AADSTS53003",  # blocked by Conditional Access
    "AADSTS65001",  # user or admin has not consented (consent withdrawn)
    "AADSTS70008",  # refresh token / code expired or revoked
    "AADSTS700082",  # refresh token expired due to inactivity
    "AADSTS700084",  # refresh token (SPA) lifetime exceeded
    "AADSTS530003",  # device must be managed (Conditional Access)
    "AADSTS7000229",  # service principal missing in the tenant (Enterprise app deleted)
    "AADSTS700016",  # application not found in the tenant
    "AADSTS7000112",  # application disabled in the tenant
}


def is_reauth_error(error: str | None, code: str | None) -> bool:
    return (error or "") in REAUTH_ERRORS or (code or "") in REAUTH_CODES


class TokenError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        error: str | None = None,
        transient: bool = False,
        tenant_id: str | None = None,
        source_id: uuid.UUID | None = None,
        reauth_required: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.error = error
        self.transient = transient
        self.tenant_id = tenant_id
        self.source_id = source_id
        # The source's owner must sign in again (Reconnect); retrying won't help.
        self.reauth_required = reauth_required


@dataclass(frozen=True)
class AppCredentials:
    client_id: str
    client_secret: str | None = None
    cert_path: str | None = None
    cert_thumbprint: str | None = None

    def assertion(self, token_url: str) -> str:
        """Signed JWT client assertion for certificate credentials."""
        assert self.cert_path
        pem = Path(self.cert_path).read_bytes()
        key = serialization.load_pem_private_key(pem, password=None)
        thumbprint = self.cert_thumbprint
        if not thumbprint:
            cert = x509.load_pem_x509_certificate(pem)
            thumbprint = cert.fingerprint(hashes.SHA1()).hex()  # noqa: S303 - required by Entra
        x5t = base64.urlsafe_b64encode(bytes.fromhex(thumbprint)).decode().rstrip("=")
        now = int(time.time())
        claims = {
            "aud": token_url,
            "iss": self.client_id,
            "sub": self.client_id,
            "jti": str(uuid.uuid4()),
            "nbf": now,
            "exp": now + 600,
        }
        return jwt.encode(claims, key, algorithm="RS256", headers={"x5t": x5t})  # type: ignore[arg-type]

    def form(self, token_url: str) -> dict[str, str]:
        if self.cert_path:
            return {
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": self.assertion(token_url),
            }
        if self.client_secret:
            return {"client_secret": self.client_secret}
        raise TokenError(f"No credentials configured for app {self.client_id}")


def source_app_credentials(settings: Settings) -> AppCredentials:
    """The multi-tenant app users sign in and link calendars with (delegated only)."""
    return AppCredentials(
        settings.source_app_client_id,
        settings.source_app_client_secret,
        settings.source_app_cert_path,
        settings.source_app_cert_thumbprint,
    )


_AADSTS = re.compile(r"AADSTS\d+")


def parse_aad_error(body: dict) -> str | None:
    codes = body.get("error_codes") or []
    if codes:
        return f"AADSTS{codes[0]}"
    match = _AADSTS.search(body.get("error_description", ""))
    return match.group(0) if match else None


@dataclass(frozen=True)
class RefreshResult:
    access_token: str
    expires_in: int
    refresh_token: str | None
    scope: str | None


class TokenService:
    def __init__(
        self,
        http: httpx.AsyncClient,
        redis: Redis,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    ):
        self.http = http
        self.redis = redis
        self.settings = settings
        self.sessionmaker = sessionmaker

    def _token_url(self, tenant_id: str) -> str:
        return f"{self.settings.login_base_url.rstrip('/')}/{tenant_id}/oauth2/v2.0/token"

    async def _post(self, url: str, data: dict[str, str], **ctx) -> dict:
        try:
            resp = await self.http.post(url, data=data)
        except httpx.TransportError as exc:
            raise TokenError(f"Token endpoint unreachable: {exc}", transient=True, **ctx) from exc
        if resp.status_code >= 500:
            raise TokenError(f"Token endpoint returned {resp.status_code}", transient=True, **ctx)
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {}
        if resp.status_code != 200 or "access_token" not in body:
            code = parse_aad_error(body)
            error = body.get("error")
            reauth = ctx.get("source_id") is not None and is_reauth_error(error, code)
            if not reauth and resp.status_code == 429:
                raise TokenError("Token endpoint throttled", transient=True, **ctx)
            raise TokenError(
                body.get("error_description") or f"HTTP {resp.status_code}",
                code=code,
                error=error,
                reauth_required=reauth,
                **ctx,
            )
        return body

    # -- per-source delegated tokens --------------------------------------------------

    @staticmethod
    def source_key(source_id: uuid.UUID) -> str:
        return f"token:source:{source_id}"

    async def _cached_source_token(self, source_id: uuid.UUID) -> str | None:
        cached = await self.redis.get(self.source_key(source_id))
        if cached:
            return cached.decode() if isinstance(cached, bytes) else cached
        return None

    async def cache_source_token(self, source_id: uuid.UUID, access_token: str, expires_in: int) -> None:
        ttl = max(int(expires_in) - REFRESH_MARGIN_SECONDS, 60)
        await self.redis.set(self.source_key(source_id), access_token, ex=ttl)

    async def invalidate_source(self, source_id: uuid.UUID) -> None:
        await self.redis.delete(self.source_key(source_id))

    async def get_source_token(self, source_id: uuid.UUID) -> str:
        """A delegated Graph token for one linked source, refreshed when needed.

        Raises TokenError(reauth_required=True) when the user must sign in again.
        """
        cached = await self._cached_source_token(source_id)
        if cached:
            return cached
        lock = self.redis.lock(
            f"lock:token:source:{source_id}",
            timeout=REFRESH_LOCK_SECONDS,
            blocking_timeout=REFRESH_LOCK_SECONDS + 5,
        )
        try:
            async with lock:
                # Another worker may have refreshed while we waited for the lock.
                cached = await self._cached_source_token(source_id)
                if cached:
                    return cached
                return await self._refresh_source(source_id)
        except LockError as exc:
            raise TokenError(
                f"Timed out waiting for a token refresh: {exc}", transient=True, source_id=source_id
            ) from exc

    async def _load_refresh_token(self, source_id: uuid.UUID) -> tuple[str | None, str | None]:
        assert self.sessionmaker is not None, "TokenService needs a sessionmaker for source tokens"
        async with self.sessionmaker() as session:
            row = (
                await session.execute(
                    select(LinkedSource.refresh_token, Tenant.tenant_id)
                    .join(Tenant, Tenant.id == LinkedSource.tenant_pk)
                    .where(LinkedSource.id == source_id)
                )
            ).first()
        if row is None:
            raise TokenError("Linked source no longer exists", source_id=source_id)
        return row[0], row[1]

    async def _refresh_source(self, source_id: uuid.UUID) -> str:
        refresh_token, tenant_id = await self._load_refresh_token(source_id)
        if not tenant_id:
            raise TokenError("Linked source has no tenant id", source_id=source_id)
        if not refresh_token:
            raise TokenError(
                "No delegated access stored for this calendar. Sign in again to reconnect it.",
                tenant_id=tenant_id,
                source_id=source_id,
                reauth_required=True,
            )
        try:
            result = await self.redeem_refresh_token(tenant_id, refresh_token, source_id=source_id)
        except TokenError as exc:
            if not exc.reauth_required:
                raise
            # The user may have reconnected (storing a new refresh token) while we were
            # using the old one. Only report reauth if the stored token didn't change.
            latest, _ = await self._load_refresh_token(source_id)
            if not latest or latest == refresh_token:
                log.warning("source %s needs reauth (%s %s)", source_id, exc.error, exc.code)
                raise
            refresh_token = latest
            result = await self.redeem_refresh_token(tenant_id, refresh_token, source_id=source_id)

        if result.refresh_token and result.refresh_token != refresh_token:
            assert self.sessionmaker is not None
            async with self.sessionmaker() as session:
                values: dict = {"refresh_token": result.refresh_token}
                if result.scope:
                    values["granted_scopes"] = result.scope
                await session.execute(
                    update(LinkedSource).where(LinkedSource.id == source_id).values(**values)
                )
                await session.commit()
        await self.cache_source_token(source_id, result.access_token, result.expires_in)
        return result.access_token

    async def redeem_refresh_token(
        self, tenant_id: str, refresh_token: str, *, source_id: uuid.UUID | None = None
    ) -> RefreshResult:
        creds = source_app_credentials(self.settings)
        url = self._token_url(tenant_id)
        body = await self._post(
            url,
            {
                "grant_type": "refresh_token",
                "client_id": creds.client_id,
                "refresh_token": refresh_token,
                "scope": SOURCE_REFRESH_SCOPE,
                **creds.form(url),
            },
            tenant_id=tenant_id,
            source_id=source_id,
        )
        return RefreshResult(
            access_token=body["access_token"],
            expires_in=int(body.get("expires_in", 3600)),
            refresh_token=body.get("refresh_token"),
            scope=body.get("scope"),
        )
