"""Delegated OpenID Connect sign-in (proof of mailbox ownership) and admin consent links."""

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt

from app.config import Settings
from app.graph.tokens import App, credentials_for

SIGN_IN_SCOPES = "openid profile email User.Read"


class OidcError(Exception):
    pass


@dataclass(frozen=True)
class SignedInIdentity:
    tenant_id: str
    object_id: str
    email: str
    name: str


def authorize_url(settings: Settings, *, state: str, nonce: str, prompt: str = "select_account") -> str:
    # Work and school accounts only; the token's `tid` tells us which tenant.
    query = urlencode(
        {
            "client_id": settings.source_app_client_id,
            "response_type": "code",
            "redirect_uri": settings.oauth_redirect_uri,
            "response_mode": "query",
            "scope": SIGN_IN_SCOPES,
            "state": state,
            "nonce": nonce,
            "prompt": prompt,
        }
    )
    return f"{settings.browser_login_base_url}/organizations/oauth2/v2.0/authorize?{query}"


def admin_consent_url(settings: Settings, *, tenant: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": settings.source_app_client_id,
            "redirect_uri": settings.admin_consent_redirect_uri,
            "state": state,
        }
    )
    return f"{settings.browser_login_base_url}/{tenant}/adminconsent?{query}"


async def exchange_code(
    http: httpx.AsyncClient, settings: Settings, *, code: str, nonce: str
) -> SignedInIdentity:
    token_url = f"{settings.login_base_url.rstrip('/')}/organizations/oauth2/v2.0/token"
    creds = credentials_for(settings, App.SOURCE)
    resp = await http.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "client_id": creds.client_id,
            "code": code,
            "redirect_uri": settings.oauth_redirect_uri,
            "scope": SIGN_IN_SCOPES,
            **creds.form(token_url),
        },
    )
    body = resp.json() if resp.content else {}
    if resp.status_code != 200 or "id_token" not in body:
        raise OidcError(body.get("error_description") or f"Token exchange failed ({resp.status_code})")
    return validate_id_token(body["id_token"], settings, nonce=nonce)


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
