"""Sign-in, source account linking (FR-3) and reconnecting a source after its delegated
grant stopped working.

Every flow is a delegated OIDC round trip against the user's own tenant. Linking and
reconnecting request Calendars.Read + offline_access, which each user consents to for
their own mailbox; the refresh token that comes back is stored (encrypted) on the source.
"""

import logging
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import current_user, get_services, get_session
from app.audit import audit
from app.graph.oidc import (
    LINK_SCOPES,
    SIGN_IN_SCOPES,
    CodeExchange,
    OidcError,
    SignedInIdentity,
    authorize_url,
    exchange_code,
    needs_admin_approval,
)
from app.models import LinkedSource, SourceStatus, Tenant, User
from app.security import (
    OAUTH_COOKIE,
    SESSION_COOKIE,
    InvalidToken,
    issue_oauth_state,
    issue_session,
    new_nonce,
    read_oauth_state,
    read_session,
    safe_next_path,
)
from app.services import Services
from app.sync.source_sync import SyncMode

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

RESULT_PATH = "/link/result"


def _web(services: Services, path: str, **params: str) -> str:
    query = f"?{urlencode(params)}" if params else ""
    return f"{services.settings.public_web_url.rstrip('/')}{path}{query}"


def _result(services: Services, status: str, **params: str) -> Response:
    return RedirectResponse(_web(services, RESULT_PATH, status=status, **params), status_code=302)


def _error(services: Services, message: str) -> Response:
    return _result(services, "error", message=message)


def _start(
    services: Services,
    *,
    purpose: str,
    user: User | None = None,
    next_path: str = "/calendar",
    source: LinkedSource | None = None,
) -> Response:
    settings = services.settings
    nonce = new_nonce()
    state = issue_oauth_state(
        settings,
        purpose=purpose,
        nonce=nonce,
        user_id=user.id if user else None,
        next_path=next_path,
        source_id=source.id if source else None,
    )
    if purpose == "login":
        url = authorize_url(settings, state=state, nonce=nonce, scopes=SIGN_IN_SCOPES)
    elif source is not None:
        # Reconnect: straight to the source's own tenant, pre-filled with its account.
        url = authorize_url(
            settings,
            state=state,
            nonce=nonce,
            scopes=LINK_SCOPES,
            tenant=source.tenant.tenant_id or "organizations",
            login_hint=source.source_email,
            prompt=None,
        )
    else:
        url = authorize_url(settings, state=state, nonce=nonce, scopes=LINK_SCOPES)
    resp = RedirectResponse(url, status_code=302)
    # Binds the OAuth round trip to this browser (login CSRF protection).
    resp.set_cookie(
        OAUTH_COOKIE,
        nonce,
        max_age=900,
        httponly=True,
        samesite="lax",
        secure=settings.cookies_secure,
        path="/auth",
    )
    return resp


@router.get("/login")
async def login(next: str | None = None, services: Services = Depends(get_services)) -> Response:
    return _start(services, purpose="login", next_path=safe_next_path(next))


@router.get("/link")
async def link(user: User = Depends(current_user), services: Services = Depends(get_services)) -> Response:
    return _start(services, purpose="link", user=user)


@router.get("/reauth/{source_id}")
async def reauth(
    source_id: uuid.UUID,
    user: User = Depends(current_user),
    services: Services = Depends(get_services),
    session: AsyncSession = Depends(get_session),
) -> Response:
    source = await session.get(LinkedSource, source_id, options=[selectinload(LinkedSource.tenant)])
    # Isolation: only the owner can reconnect a source (and learns nothing about others).
    if source is None or source.user_id != user.id:
        return _error(services, "Calendar not found")
    if source.status == SourceStatus.REMOVING:
        return _error(services, "This calendar is being unlinked")
    return _start(services, purpose="reauth", user=user, source=source)


@router.post("/logout")
async def logout(services: Services = Depends(get_services)) -> Response:
    resp = Response(status_code=204)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    services: Services = Depends(get_services),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if error:
        log.info("sign-in error: %s %s", error, error_description)
        if needs_admin_approval(error, error_description):
            return _result(
                services,
                "admin_approval_required",
                message=error_description
                or "Your organization requires an administrator to approve Calendar Sync.",
            )
        return _error(services, error_description or error)
    try:
        claims = read_oauth_state(services.settings, state or "")
    except InvalidToken:
        return _error(services, "Sign-in expired, try again")
    nonce = request.cookies.get(OAUTH_COOKIE)
    if not nonce or nonce != claims["nonce"] or not code:
        return _error(services, "Sign-in could not be verified")

    purpose = claims["purpose"]
    reauth_source: LinkedSource | None = None
    if purpose == "reauth":
        try:
            source_id = uuid.UUID(claims.get("sid", ""))
        except ValueError:
            return _error(services, "Sign-in could not be verified")
        reauth_source = await session.get(
            LinkedSource, source_id, options=[selectinload(LinkedSource.tenant)]
        )
        if reauth_source is None:
            return _error(services, "This calendar is no longer linked")
    try:
        result = await exchange_code(
            services.http,
            services.settings,
            code=code,
            nonce=nonce,
            scopes=SIGN_IN_SCOPES if purpose == "login" else LINK_SCOPES,
            tenant=(reauth_source.tenant.tenant_id if reauth_source else None) or "organizations",
        )
    except OidcError as exc:
        return _error(services, str(exc))

    if purpose == "login":
        resp = await _complete_login(services, session, result.identity, claims.get("next", "/calendar"))
    elif purpose == "reauth" and reauth_source is not None:
        resp = await _complete_reauth(request, services, session, result, claims.get("uid"), reauth_source)
    else:
        resp = await _complete_link(request, services, session, result, claims.get("uid"))
    resp.delete_cookie(OAUTH_COOKIE, path="/auth")
    return resp


async def _complete_login(
    services: Services, session: AsyncSession, identity: SignedInIdentity, next_path: str
) -> Response:
    settings = services.settings
    user = await session.scalar(
        select(User).where(
            User.login_tenant_id == identity.tenant_id, User.login_object_id == identity.object_id
        )
    )
    is_new = user is None
    if user is None:
        user = User(
            email=identity.email,
            display_name=identity.name,
            login_tenant_id=identity.tenant_id,
            login_object_id=identity.object_id,
        )
        session.add(user)
    user.email, user.display_name = identity.email, identity.name
    if identity.email in settings.operator_email_set:
        user.is_operator = True
    await session.flush()
    audit(session, "user.signed_up" if is_new else "user.signed_in", "user", user.id, actor=user)
    await session.commit()

    resp = RedirectResponse(_web(services, safe_next_path(next_path)), status_code=302)
    resp.set_cookie(
        SESSION_COOKIE,
        issue_session(settings, user.id),
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.cookies_secure,
        path="/",
    )
    return resp


async def _session_user(
    request: Request, services: Services, session: AsyncSession, state_uid: str | None
) -> User | Response:
    """The user who started linking/reconnecting must still be the one signed in."""
    try:
        session_uid = read_session(services.settings, request.cookies.get(SESSION_COOKIE, ""))
    except InvalidToken:
        return _error(services, "Please sign in again")
    if str(session_uid) != state_uid:
        return _error(services, "Session mismatch")
    user = await session.get(User, session_uid)
    if user is None:
        return _error(services, "Unknown user")
    return user


def _grant_problem(result: CodeExchange) -> str | None:
    if not result.can_read_calendars:
        return "Calendar access wasn't granted. Try again and accept the permission request."
    if not result.refresh_token:
        return "Microsoft didn't grant offline access to your calendar. Try again."
    return None


async def _activate(
    services: Services,
    session: AsyncSession,
    source: LinkedSource,
    result: CodeExchange,
    *,
    user: User,
    action: str,
    mode: SyncMode,
) -> None:
    """Store the fresh delegated grant, (re)activate the source and start syncing it."""
    source.source_email = result.identity.email
    source.refresh_token = result.refresh_token
    source.granted_scopes = result.scope
    source.status = SourceStatus.ACTIVE
    source.reauth_required_at = None
    source.last_error = None
    source.last_error_at = None
    await session.flush()
    audit(
        session,
        action,
        "linked_source",
        source.id,
        actor=user,
        email=result.identity.email,
        tenant=source.tenant_pk,
    )
    await session.commit()
    # Drop any access token minted from the previous grant; seed the new one.
    await services.tokens.invalidate_source(source.id)
    if result.access_token:
        await services.tokens.cache_source_token(source.id, result.access_token, result.expires_in)
    # Subscribe (replacing any subscription from the old grant), then sync.
    await services.queue.enqueue("setup_source", str(source.id), mode.value)


async def _complete_link(
    request: Request,
    services: Services,
    session: AsyncSession,
    result: CodeExchange,
    state_uid: str | None,
) -> Response:
    user = await _session_user(request, services, session, state_uid)
    if isinstance(user, Response):
        return user
    identity = result.identity

    if problem := _grant_problem(result):
        return _error(services, problem)

    tenant = await session.scalar(select(Tenant).where(Tenant.tenant_id == identity.tenant_id))
    domain = identity.email.split("@")[-1]
    if tenant is None:
        tenant = Tenant(tenant_id=identity.tenant_id, display_name=domain, domain=domain)
        session.add(tenant)
        await session.flush()
        audit(session, "tenant.discovered", "tenant", tenant.id, actor=user, tenant_id=identity.tenant_id)

    # Proof of ownership: the user just signed in to this exact mailbox (FR-3).
    source = await session.scalar(
        select(LinkedSource).where(
            LinkedSource.user_id == user.id,
            LinkedSource.tenant_pk == tenant.id,
            LinkedSource.source_user_id == identity.object_id,
        )
    )
    if source is None:
        source = LinkedSource(
            user_id=user.id,
            tenant_pk=tenant.id,
            source_user_id=identity.object_id,
            source_email=identity.email,
            label=tenant.display_name if tenant.display_name != domain else domain.split(".")[0].title(),
        )
        session.add(source)
        await session.flush()
        await _activate(
            services, session, source, result, user=user, action="source.linked", mode=SyncMode.INITIAL
        )
        return _result(services, "linked", email=identity.email)

    if source.status == SourceStatus.REMOVING:
        return _error(services, "This account is still being unlinked, try again shortly")
    reconnected = source.status == SourceStatus.REAUTH_REQUIRED
    await _activate(
        services,
        session,
        source,
        result,
        user=user,
        action="source.reconnected" if reconnected else "source.relinked",
        mode=SyncMode.RECOVERY,
    )
    return _result(services, "reconnected" if reconnected else "linked", email=identity.email)


async def _complete_reauth(
    request: Request,
    services: Services,
    session: AsyncSession,
    result: CodeExchange,
    state_uid: str | None,
    source: LinkedSource,
) -> Response:
    user = await _session_user(request, services, session, state_uid)
    if isinstance(user, Response):
        return user
    if source.user_id != user.id:
        return _error(services, "This calendar is no longer linked")
    if source.status == SourceStatus.REMOVING:
        return _error(services, "This calendar is being unlinked")
    identity = result.identity
    if identity.tenant_id != source.tenant.tenant_id or identity.object_id != source.source_user_id:
        log.info("reauth for source %s signed in as a different account", source.id)
        return _error(
            services,
            f"You signed in as {identity.email}. "
            f"Sign in as {source.source_email} to reconnect this calendar.",
        )
    if problem := _grant_problem(result):
        return _error(services, problem)
    await _activate(
        services, session, source, result, user=user, action="source.reconnected", mode=SyncMode.RECOVERY
    )
    return _result(services, "reconnected", email=identity.email)
