"""arq job functions. Each one is a thin wrapper around app.sync.* run through `run_job`,
which centralises coalescing, throttling, retries, revocation and error recording."""

import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from arq import Retry
from sqlalchemy.orm import selectinload

from app.graph.client import GraphError, GraphForbidden, GraphThrottled
from app.graph.tokens import TokenError
from app.metrics import FETCH_ERRORS, JOB_FAILURES, THROTTLED
from app.models import LinkedSource, SyncError
from app.queue import (
    PENDING_TTL_SECONDS,
    lifecycle_key,
    notification_key,
    reconcile_key,
    renew_key,
    sync_key,
)
from app.services import Services
from app.sync import lifecycle, mirror, source_sync, subscriptions
from app.sync.source_sync import SyncMode
from app.timeutil import utcnow

log = logging.getLogger(__name__)

MAX_TRIES = 25


def _backoff(job_try: int) -> float:
    return min(5 * 2 ** (job_try - 1), 600) + random.uniform(0, 3)


async def record_error(
    services: Services,
    kind: str,
    exc: BaseException,
    *,
    source_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> None:
    message = str(exc) or exc.__class__.__name__
    async with services.sessionmaker() as session:
        tenant_pk = None
        if source_id:
            source = await session.get(LinkedSource, source_id, options=[selectinload(LinkedSource.tenant)])
            if source is not None:
                source.last_error = message[:2000]
                source.last_error_at = utcnow()
                tenant_pk, user_id = source.tenant_pk, source.user_id
                if source.tenant.tenant_id:
                    FETCH_ERRORS.labels(source.tenant.tenant_id, kind).inc()
            else:
                source_id = None
        session.add(
            SyncError(
                user_id=user_id, tenant_pk=tenant_pk, source_id=source_id, kind=kind, message=message[:4000]
            )
        )
        await session.commit()


async def run_job(
    ctx: dict[str, Any],
    name: str,
    fn: Callable[[Services], Awaitable[Any]],
    *,
    key: str | None = None,
    source_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> Any:
    services: Services = ctx["services"]
    job_try: int = ctx.get("job_try", 1)
    if key:
        await services.queue.release(key)

    async def retry(defer: float) -> Any:
        # If a newer identical job was enqueued while we ran, let it carry the work.
        if key and not await services.redis.set(f"pending:{key}", "1", nx=True, ex=PENDING_TTL_SECONDS):
            return "superseded"
        if job_try >= MAX_TRIES:
            raise RuntimeError(f"{name} gave up after {job_try} tries")
        raise Retry(defer=defer)

    try:
        return await fn(services)
    except GraphThrottled as exc:
        THROTTLED.labels(exc.scope.split(":", 1)[0]).inc()
        return await retry(exc.retry_after + random.uniform(0, 2))
    except TokenError as exc:
        if exc.revoked and exc.tenant_id:
            log.warning("tenant %s revoked access (%s)", exc.tenant_id, exc.code)
            await services.queue.enqueue("revoke_tenant_by_entra_id", exc.tenant_id, f"{exc.code}: {exc}")
            return "tenant-revoked"
        if exc.transient:
            return await retry(_backoff(job_try))
        await record_error(services, f"{name}:token", exc, source_id=source_id, user_id=user_id)
        JOB_FAILURES.labels(name).inc()
        raise
    except (httpx.TransportError, GraphError) as exc:
        transient = isinstance(exc, httpx.TransportError) or (isinstance(exc, GraphError) and exc.transient)
        if transient and job_try < MAX_TRIES:
            return await retry(_backoff(job_try))
        kind = f"{name}:forbidden" if isinstance(exc, GraphForbidden) else name
        await record_error(services, kind, exc, source_id=source_id, user_id=user_id)
        JOB_FAILURES.labels(name).inc()
        raise
    except Exception as exc:
        log.exception("job %s failed", name)
        await record_error(services, name, exc, source_id=source_id, user_id=user_id)
        JOB_FAILURES.labels(name).inc()
        raise


# --- event jobs ----------------------------------------------------------------------


async def process_notification(
    ctx: dict, graph_subscription_id: str, resource_id: str, change_type: str
) -> Any:
    services: Services = ctx["services"]
    source_id = await source_sync.source_for_subscription(services, graph_subscription_id)
    return await run_job(
        ctx,
        "process_notification",
        lambda s: source_sync.process_notification(s, graph_subscription_id, resource_id),
        key=notification_key(graph_subscription_id, resource_id),
        source_id=source_id,
    )


async def sync_source(ctx: dict, source_id: str, mode: str) -> Any:
    sid = uuid.UUID(source_id)

    async def run(s: Services) -> str:
        result = await source_sync.sync_source(s, sid, SyncMode(mode))
        return (
            result.skipped
            or f"pages={result.pages} changed={len(result.changed_keys)} full={result.rebaseline}"
        )

    return await run_job(ctx, "sync_source", run, key=sync_key(sid, mode), source_id=sid)


async def reconcile_group(ctx: dict, user_id: str, dedup_key: str, force: bool = False) -> Any:
    uid = uuid.UUID(user_id)
    return await run_job(
        ctx,
        "reconcile_group",
        lambda s: mirror.reconcile_group(s, uid, dedup_key, force=force),
        key=None if force else reconcile_key(uid, dedup_key),
        user_id=uid,
    )


async def rebuild_user(ctx: dict, user_id: str) -> Any:
    """Operator recovery: re-check every mirror, recreating any deleted from Outlook."""
    uid = uuid.UUID(user_id)

    async def run(s: Services) -> str:
        async with s.sessionmaker() as session:
            keys = await mirror.all_dedup_keys(session, uid)
        for key in keys:
            await s.queue.enqueue("reconcile_group", user_id, key, True)
        return f"queued={len(keys)}"

    return await run_job(ctx, "rebuild_user", run, user_id=uid)


# --- subscription jobs ---------------------------------------------------------------


async def setup_source(ctx: dict, source_id: str) -> Any:
    sid = uuid.UUID(source_id)
    return await run_job(ctx, "setup_source", lambda s: lifecycle.setup_source(s, sid), source_id=sid)


async def ensure_subscription(ctx: dict, source_id: str) -> Any:
    sid = uuid.UUID(source_id)

    async def run(s: Services) -> str:
        sub = await subscriptions.create_subscription(s, sid)
        if sub is None:
            return "skipped"
        await s.queue.enqueue_coalesced(
            sync_key(sid, SyncMode.RECOVERY), "sync_source", source_id, SyncMode.RECOVERY.value
        )
        return "created"

    return await run_job(ctx, "ensure_subscription", run, source_id=sid)


async def renew_subscription(ctx: dict, subscription_pk: str) -> Any:
    pk = uuid.UUID(subscription_pk)
    return await run_job(
        ctx, "renew_subscription", lambda s: subscriptions.renew_subscription(s, pk), key=renew_key(pk)
    )


async def handle_lifecycle(ctx: dict, graph_subscription_id: str, event: str) -> Any:
    services: Services = ctx["services"]
    source_id = await source_sync.source_for_subscription(services, graph_subscription_id)
    return await run_job(
        ctx,
        "handle_lifecycle",
        lambda s: subscriptions.handle_lifecycle(s, graph_subscription_id, event),
        key=lifecycle_key(graph_subscription_id, event),
        source_id=source_id,
    )


# --- source / tenant / user lifecycle ------------------------------------------------


async def remove_source(ctx: dict, source_id: str, reason: str = "unlinked") -> Any:
    sid = uuid.UUID(source_id)
    return await run_job(
        ctx, "remove_source", lambda s: lifecycle.purge_source(s, sid, final_status=None, reason=reason)
    )


async def revoke_tenant(ctx: dict, tenant_pk: str, reason: str) -> Any:
    pk = uuid.UUID(tenant_pk)
    return await run_job(ctx, "revoke_tenant", lambda s: lifecycle.revoke_tenant(s, pk, reason))


async def revoke_tenant_by_entra_id(ctx: dict, tenant_id: str, reason: str) -> Any:
    return await run_job(
        ctx, "revoke_tenant", lambda s: lifecycle.revoke_tenant_by_entra_id(s, tenant_id, reason)
    )


async def activate_tenant(ctx: dict, tenant_pk: str) -> Any:
    pk = uuid.UUID(tenant_pk)
    return await run_job(ctx, "activate_tenant", lambda s: lifecycle.activate_tenant_sources(s, pk))


async def provision_primary(ctx: dict, user_id: str) -> Any:
    uid = uuid.UUID(user_id)
    return await run_job(ctx, "provision_primary", lambda s: lifecycle.provision_primary(s, uid), user_id=uid)


async def delete_user(ctx: dict, user_id: str) -> Any:
    uid = uuid.UUID(user_id)
    return await run_job(ctx, "delete_user", lambda s: lifecycle.delete_user(s, uid))


JOB_FUNCTIONS = [
    process_notification,
    sync_source,
    reconcile_group,
    rebuild_user,
    setup_source,
    ensure_subscription,
    renew_subscription,
    handle_lifecycle,
    remove_source,
    revoke_tenant,
    revoke_tenant_by_entra_id,
    activate_tenant,
    provision_primary,
    delete_user,
]
