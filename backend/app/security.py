"""Session cookies and signed OAuth state."""

import secrets
import uuid
from datetime import timedelta
from typing import Any

import jwt

from app.config import Settings
from app.timeutil import utcnow

SESSION_COOKIE = "cs_session"
OAUTH_COOKIE = "cs_oauth"
_ALG = "HS256"


class InvalidToken(Exception):
    pass


def _encode(settings: Settings, claims: dict[str, Any], ttl: timedelta) -> str:
    now = utcnow()
    return jwt.encode({**claims, "iat": now, "exp": now + ttl}, settings.session_secret, algorithm=_ALG)


def _decode(settings: Settings, token: str, typ: str) -> dict[str, Any]:
    try:
        claims = jwt.decode(token, settings.session_secret, algorithms=[_ALG])
    except jwt.PyJWTError as exc:
        raise InvalidToken(str(exc)) from exc
    if claims.get("typ") != typ:
        raise InvalidToken("Wrong token type")
    return claims


def issue_session(settings: Settings, user_id: uuid.UUID) -> str:
    return _encode(
        settings, {"typ": "session", "sub": str(user_id)}, timedelta(hours=settings.session_ttl_hours)
    )


def read_session(settings: Settings, token: str) -> uuid.UUID:
    return uuid.UUID(_decode(settings, token, "session")["sub"])


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


def issue_oauth_state(
    settings: Settings,
    *,
    purpose: str,
    nonce: str,
    user_id: uuid.UUID | None = None,
    next_path: str = "/",
    source_id: uuid.UUID | None = None,
) -> str:
    """purpose: "login" (platform sign-in), "link" (add a source) or "reauth" (reconnect
    the source `source_id`)."""
    claims = {"typ": "oauth", "purpose": purpose, "nonce": nonce, "next": next_path}
    if user_id:
        claims["uid"] = str(user_id)
    if source_id:
        claims["sid"] = str(source_id)
    return _encode(settings, claims, timedelta(minutes=15))


def read_oauth_state(settings: Settings, token: str) -> dict[str, Any]:
    return _decode(settings, token, "oauth")


def safe_next_path(value: str | None) -> str:
    """Only allow same-site relative redirects."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/dashboard"
    return value
