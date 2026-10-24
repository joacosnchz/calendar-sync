"""Delegated OpenID Connect sign-in.

Platform sign-in only proves who the user is (openid profile email User.Read). Linking a
source calendar (and reconnecting one) asks the mailbox owner for delegated
Calendars.Read plus offline_access (incremental consent), which every user can grant for
themselves: no tenant admin is involved. The resulting refresh token is stored per
source and used by the workers (see app.graph.tokens).
"""

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt

from app.config import Settings
from app.graph.tokens import source_app_credentials

SIGN_IN_SCOPES = "openid profile email User.Read"
LINK_SCOPES = "openid profile email offline_access User.Read Calendars.Read"
REQUIRED_SOURCE_SCOPE = "calendars.read"

# Authorize-endpoint errors meaning the tenant doesn't let users consent for themselves
# (an admin has to approve the app, or user consent is disabled).
ADMIN_APPROVAL_CODES = ("AADSTS90094", "AADSTS90008")


class OidcError(Exception):
    pass


@dataclass(frozen=True)
class SignedInIdentity:
    tenant_id: str
    object_id: str
    email: str
    name: str


@dataclass(frozen=True)
class CodeExchange:
    identity: SignedInIdentity
    access_token: str | None
    expires_in: int
    refresh_token: str | None
    scope: str

    @property
    def can_read_calendars(self) -> bool:
        granted = {s.rsplit("/", 1)[-1].lower() for s in self.scope.split()}
        return REQUIRED_SOURCE_SCOPE in granted


def needs_admin_approval(error: str | None, description: str | None) -> bool:
    description = description or ""
    if any(code in description for code in ADMIN_APPROVAL_CODES):
        return True
    if "AADSTS65001" in description and "admin" in description.lower():
        return True
    return error == "consent_required"


def authorize_url(
    settings: Settings,
    *,
    state: str,
    nonce: str,
    scopes: str = SIGN_IN_SCOPES,
    tenant: str = "organizations",
    login_hint: str | None = None,
    prompt: str | None = "select_account",
) -> str:
    # Work and school accounts only; the token's `tid` tells us which tenant.
    params = {
        "client_id": settings.source_app_client_id,
        "response_type": "code",
        "redirect_uri": settings.oauth_redirect_uri,
        "response_mode": "query",
        "scope": scopes,
        "state": state,
        "nonce": nonce,
    }
    if prompt:
        params["prompt"] = prompt
    if login_hint:
        params["login_hint"] = login_hint
    return f"{settings.browser_login_base_url}/{tenant}/oauth2/v2.0/authorize?{urlencode(params)}"


async def exchange_code(
    http: httpx.AsyncClient,
    settings: Settings,
    *,
    code: str,
    nonce: str,
    scopes: str = SIGN_IN_SCOPES,
    tenant: str = "organizations",
) -> CodeExchange:
    token_url = f"{settings.login_base_url.rstrip('/')}/{tenant}/oauth2/v2.0/token"
    creds = source_app_credentials(settings)
    try:
        resp = await http.post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "client_id": creds.client_id,
                "code": code,
                "redirect_uri": settings.oauth_redirect_uri,
                "scope": scopes,
                **creds.form(token_url),
            },
        )
    except httpx.TransportError as exc:
        raise OidcError("Microsoft sign-in is unreachable, try again") from exc
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}
    if resp.status_code != 200 or "id_token" not in body:
        raise OidcError(body.get("error_description") or f"Token exchange failed ({resp.status_code})")
    return CodeExchange(
        identity=validate_id_token(body["id_token"], settings, nonce=nonce),
        access_token=body.get("access_token"),
        expires_in=int(body.get("expires_in", 3600)),
        refresh_token=body.get("refresh_token"),
        scope=body.get("scope", ""),
    )


def validate_id_token(id_token: str, settings: Settings, *, nonce: str) -> SignedInIdentity:
    # The ID token came straight from the token endpoint over TLS (authorization code
    # flow), so per OIDC Core 3.1.3.7 TLS authenticates the issuer and the signature check
    # may be skipped. Every claim is still validated.
    try:
        claims = jwt.decode(
            id_token,
            options={"verify_signature": False, "require": ["exp", "aud", "iss", "tid", "oid"]},
            audience=settings.source_app_client_id,
        )
    except jwt.PyJWTError as exc:
        raise OidcError(f"Invalid ID token: {exc}") from exc
    tid, oid = claims["tid"], claims["oid"]
    expected_iss = f"{settings.login_base_url.rstrip('/')}/{tid}/v2.0"
    if claims["iss"] != expected_iss:
        raise OidcError("ID token issuer mismatch")
    if claims.get("nonce") != nonce:
        raise OidcError("ID token nonce mismatch")
    email = (claims.get("email") or claims.get("preferred_username") or "").lower()
    if not email:
        raise OidcError("ID token has no email or preferred_username")
    return SignedInIdentity(tenant_id=tid, object_id=oid, email=email, name=claims.get("name") or email)
