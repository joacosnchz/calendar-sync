"""Sign-in, source account linking (FR-3) and tenant admin consent (FR-1)."""

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, get_services, get_session
from app.audit import audit
from app.graph.oidc import OidcError, SignedInIdentity, authorize_url, exchange_code
from app.models import ConsentStatus, LinkedSource, PrimaryStatus, SourceStatus, Tenant, User
from app.security import (
    OAUTH_COOKIE,
    SESSION_COOKIE,
    InvalidToken,
    issue_oauth_state,
    issue_session,
    new_nonce,
    read_consent_state,
    read_oauth_state,
    read_session,
    safe_next_path,
)
from app.services import Services
from app.timeutil import utcnow

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _web(services: Services, path: str, **params: str) -> str:
    query = f"?{urlencode(params)}" if params else ""
    return f"{services.settings.public_web_url.rstrip('/')}{path}{query}"


def _start(
    services: Services, *, purpose: str, user: User | None = None, next_path: str = "/dashboard"
) -> Response:
    nonce = new_nonce()
    state = issue_oauth_state(
        services.settings,
        purpose=purpose,
        nonce=nonce,
        user_id=user.id if user else None,
        next_path=next_path,
    )
    resp = RedirectResponse(authorize_url(services.settings, state=state, nonce=nonce), status_code=302)
    # Binds the OAuth round trip to this browser (login CSRF protection).
    resp.set_cookie(
        OAUTH_COOKIE,
        nonce,
        max_age=900,
        httponly=True,
        samesite="lax",
        secure=services.settings.cookies_secure,
        path="/auth",
    )
    return resp


@router.get("/login")
async def login(next: str | None = None, services: Services = Depends(get_services)) -> Response:
    return _start(services, purpose="login", next_path=safe_next_path(next))


@router.get("/link")
async def link(user: User = Depends(current_user), services: Services = Depends(get_services)) -> Response:
    return _start(services, purpose="link", user=user)


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
        return RedirectResponse(
            _web(services, "/link/result", status="error", message=error_description or error)
        )
    try:
        claims = read_oauth_state(services.settings, state or "")
    except InvalidToken:
        return RedirectResponse(
            _web(services, "/link/result", status="error", message="Sign-in expired, try again")
        )
    nonce = request.cookies.get(OAUTH_COOKIE)
    if not nonce or nonce != claims["nonce"] or not code:
        return RedirectResponse(
            _web(services, "/link/result", status="error", message="Sign-in could not be verified")
        )
    try:
        identity = await exchange_code(services.http, services.settings, code=code, nonce=nonce)
    except OidcError as exc:
        return RedirectResponse(_web(services, "/link/result", status="error", message=str(exc)))

    if claims["purpose"] == "login":
        resp = await _complete_login(services, session, identity, claims.get("next", "/dashboard"))
    else:
        resp = await _complete_link(request, services, session, identity, claims.get("uid"))
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
            primary_status=(
                PrimaryStatus.PROVISIONING
                if settings.primary_calendar_mode == "shared_mailbox"
                else PrimaryStatus.UNASSIGNED
            ),
        )
        session.add(user)
    user.email, user.display_name = identity.email, identity.name
    if identity.email in settings.operator_email_set:
        user.is_operator = True
    await session.flush()
    audit(session, "user.signed_up" if is_new else "user.signed_in", "user", user.id, actor=user)
    await session.commit()
    if is_new and user.primary_status == PrimaryStatus.PROVISIONING:
        await services.queue.enqueue("provision_primary", str(user.id))

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


async def _complete_link(
    request: Request,
    services: Services,
    session: AsyncSession,
    identity: SignedInIdentity,
    state_uid: str | None,
) -> Response:
    settings = services.settings
    # The user who started linking must still be the one signed in to the platform.
    try:
        session_uid = read_session(settings, request.cookies.get(SESSION_COOKIE, ""))
    except InvalidToken:
        return RedirectResponse(
            _web(services, "/link/result", status="error", message="Please sign in again")
        )
    if str(session_uid) != state_uid:
        return RedirectResponse(_web(services, "/link/result", status="error", message="Session mismatch"))
    user = await session.get(User, session_uid)
    if user is None:
        return RedirectResponse(_web(services, "/link/result", status="error", message="Unknown user"))

    if identity.tenant_id == settings.home_tenant_id:
        # Primary calendars live in our tenant; linking it as a source would mirror mirrors.
        return RedirectResponse(
            _web(
                services,
                "/link/result",
                status="error",
                message="Accounts in the host tenant can't be linked",
            )
        )

    tenant = await session.scalar(select(Tenant).where(Tenant.tenant_id == identity.tenant_id))
    domain = identity.email.split("@")[-1]
    if tenant is None:
        tenant = Tenant(tenant_id=identity.tenant_id, display_name=domain, domain=domain)
        session.add(tenant)
        await session.flush()
        audit(session, "tenant.discovered", "tenant", tenant.id, actor=user, tenant_id=identity.tenant_id)

    connected = tenant.consent_status == ConsentStatus.CONNECTED
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
    elif source.status == SourceStatus.REMOVING:
        return RedirectResponse(
            _web(
                services,
                "/link/result",
                status="error",
                message="This account is still being unlinked, try again shortly",
            )
        )
    source.source_email = identity.email
    source.status = SourceStatus.ACTIVE if connected else SourceStatus.PENDING_CONSENT
    source.last_error = None
    await session.flush()
    audit(
        session,
        "source.linked",
        "linked_source",
        source.id,
        actor=user,
        email=identity.email,
        tenant=tenant.id,
    )
    await session.commit()

    if not connected:
        return RedirectResponse(
            _web(
                services,
                "/link/result",
                status="consent_required",
                source=str(source.id),
                email=identity.email,
            )
        )
    await services.queue.enqueue("setup_source", str(source.id))
    return RedirectResponse(_web(services, "/link/result", status="linked", email=identity.email))


@router.get("/adminconsent/callback")
async def admin_consent_callback(
    state: str | None = None,
    tenant: str | None = None,
    admin_consent: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    services: Services = Depends(get_services),
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        tenant_pk = read_consent_state(services.settings, state or "")
    except InvalidToken:
        return RedirectResponse(
            _web(services, "/consent/result", status="error", message="Consent link expired")
        )
    invite = await session.get(Tenant, tenant_pk)
    if invite is None:
        return RedirectResponse(
            _web(services, "/consent/result", status="error", message="Unknown consent request")
        )

    if error or (admin_consent or "").lower() != "true" or not tenant:
        invite.consent_status = ConsentStatus.DENIED
        invite.last_error = error_description or error or "Consent not granted"
        audit(session, "tenant.consent_denied", "tenant", invite.id, actor="graph", error=error)
        await session.commit()
        return RedirectResponse(
            _web(services, "/consent/result", status="denied", message=error_description or error or "")
        )

    if tenant == services.settings.home_tenant_id:
        return RedirectResponse(
            _web(
                services,
                "/consent/result",
                status="error",
                message="The host tenant can't be a source tenant",
            )
        )

    target = invite
    existing = await session.scalar(select(Tenant).where(Tenant.tenant_id == tenant))
    if existing is not None and existing.id != invite.id:
        # A user from this tenant tried to link before consent; fold the invite into it.
        await session.execute(
            update(LinkedSource).where(LinkedSource.tenant_pk == invite.id).values(tenant_pk=existing.id)
        )
        if existing.display_name == existing.domain:
            existing.display_name = invite.display_name
        await session.delete(invite)
        target = existing
    elif invite.tenant_id and invite.tenant_id != tenant:
        return RedirectResponse(
            _web(
                services,
                "/consent/result",
                status="error",
                message="Consent came from a different tenant than expected",
            )
        )

    target.tenant_id = tenant
    target.consent_status = ConsentStatus.CONNECTED
    target.connected_at = utcnow()
    target.revoked_at = None
    target.last_error = None
    audit(session, "tenant.connected", "tenant", target.id, actor="graph", tenant_id=tenant)
    await session.commit()
    await services.queue.enqueue("activate_tenant", str(target.id))
    return RedirectResponse(_web(services, "/consent/result", status="connected", tenant=target.display_name))
