"""App-only tokens per (app, tenant) via the client credentials flow.

Tokens are requested from the target tenant's endpoint (never `common`) and cached in
Redis, so every worker shares one token per tenant and refreshes it before it expires.
"""

import base64
import logging
import re
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from redis.asyncio import Redis

from app.config import Settings

log = logging.getLogger(__name__)

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
REFRESH_MARGIN_SECONDS = 300

# AADSTS codes meaning the customer removed our app (or never consented) rather than a
# transient failure. Only these mark a tenant revoked, so a blip never deletes mirrors.
REVOCATION_CODES = {
    "AADSTS7000229",  # missing service principal in the tenant (Enterprise app deleted)
    "AADSTS700016",  # application not found in the directory
    "AADSTS65001",  # consent not granted
    "AADSTS7000112",  # application disabled
}


class App(StrEnum):
    SOURCE = "source"  # multi-tenant, Calendars.Read
    PRIMARY = "primary"  # our tenant only, Calendars.ReadWrite


class TokenError(Exception):
    def __init__(
        self, message: str, *, code: str | None = None, transient: bool = False, tenant_id: str | None = None
    ):
        super().__init__(message)
        self.code = code
        self.transient = transient
        self.tenant_id = tenant_id

    @property
    def revoked(self) -> bool:
        return self.code in REVOCATION_CODES


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


def credentials_for(settings: Settings, app: App) -> AppCredentials:
    if app is App.SOURCE:
        return AppCredentials(
            settings.source_app_client_id,
            settings.source_app_client_secret,
            settings.source_app_cert_path,
            settings.source_app_cert_thumbprint,
        )
    return AppCredentials(
        settings.primary_app_client_id,
        settings.primary_app_client_secret,
        settings.primary_app_cert_path,
        settings.primary_app_cert_thumbprint,
    )


_AADSTS = re.compile(r"AADSTS\d+")


def parse_aad_error(body: dict) -> str | None:
    codes = body.get("error_codes") or []
    if codes:
        return f"AADSTS{codes[0]}"
    match = _AADSTS.search(body.get("error_description", ""))
    return match.group(0) if match else None


class TokenService:
    def __init__(self, http: httpx.AsyncClient, redis: Redis, settings: Settings):
        self.http = http
        self.redis = redis
        self.settings = settings

    def _key(self, app: App, tenant_id: str) -> str:
        return f"token:{app}:{tenant_id}"

    async def get_token(self, app: App, tenant_id: str) -> str:
        if app is App.PRIMARY and tenant_id != self.settings.home_tenant_id:
            raise TokenError("The write app only has access to our own tenant")
        cached = await self.redis.get(self._key(app, tenant_id))
        if cached:
            return cached.decode() if isinstance(cached, bytes) else cached
        token, expires_in = await self._request(app, tenant_id)
        ttl = max(int(expires_in) - REFRESH_MARGIN_SECONDS, 60)
        await self.redis.set(self._key(app, tenant_id), token, ex=ttl)
        return token

    async def invalidate(self, app: App, tenant_id: str) -> None:
        await self.redis.delete(self._key(app, tenant_id))

    async def _request(self, app: App, tenant_id: str) -> tuple[str, int]:
        creds = credentials_for(self.settings, app)
        url = f"{self.settings.login_base_url.rstrip('/')}/{tenant_id}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": creds.client_id,
            "scope": GRAPH_SCOPE,
            **creds.form(url),
        }
        try:
            resp = await self.http.post(url, data=data)
        except httpx.TransportError as exc:
            raise TokenError(
                f"Token endpoint unreachable: {exc}", transient=True, tenant_id=tenant_id
            ) from exc
        if resp.status_code >= 500:
            raise TokenError(
                f"Token endpoint returned {resp.status_code}", transient=True, tenant_id=tenant_id
            )
        body = resp.json() if resp.content else {}
        if resp.status_code != 200 or "access_token" not in body:
            code = parse_aad_error(body)
            log.warning("token request failed app=%s tenant=%s code=%s", app, tenant_id, code)
            raise TokenError(
                body.get("error_description") or f"HTTP {resp.status_code}", code=code, tenant_id=tenant_id
            )
        return body["access_token"], int(body.get("expires_in", 3600))
