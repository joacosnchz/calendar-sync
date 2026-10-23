"""Graph change and lifecycle notification receivers.

These endpoints only validate and enqueue: every Graph read happens in the workers, so a
slow fetch never delays the acknowledgement Graph is waiting for (202 in < 3 s).
"""

import hmac
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_services, get_session
from app.metrics import LIFECYCLE_RECEIVED, NOTIFICATIONS_RECEIVED, NOTIFICATIONS_REJECTED, WEBHOOK_LATENCY
from app.models import LinkedSource, Subscription, Tenant
from app.queue import lifecycle_key, notification_key
from app.services import Services

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class Notification(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    subscription_id: str = Field(alias="subscriptionId")
    client_state: str | None = Field(default=None, alias="clientState")
    tenant_id: str | None = Field(default=None, alias="tenantId")
    change_type: str | None = Field(default=None, alias="changeType")
    resource: str | None = None
    resource_data: dict[str, Any] | None = Field(default=None, alias="resourceData")
    lifecycle_event: str | None = Field(default=None, alias="lifecycleEvent")

    @property
    def resource_id(self) -> str | None:
        if self.resource_data and self.resource_data.get("id"):
            return self.resource_data["id"]
        if self.resource:
            return self.resource.rstrip("/").split("/")[-1].removeprefix("Events('").removesuffix("')")
        return None


def _validation_response(token: str) -> Response:
    # Handshake: echo the token as text/plain within 10 seconds.
    return PlainTextResponse(token, status_code=200)


async def _parse(request: Request) -> list[Notification]:
    try:
        body = await request.json()
        items = body.get("value", [])
    except (ValueError, AttributeError) as exc:
        raise HTTPException(400, "Invalid notification payload") from exc
    parsed = []
    for item in items:
        try:
            parsed.append(Notification.model_validate(item))
        except ValidationError:
            NOTIFICATIONS_REJECTED.labels("malformed").inc()
    return parsed


async def _verified(
    session: AsyncSession, notifications: list[Notification]
) -> list[tuple[Notification, str]]:
    """Keep notifications whose clientState matches the secret stored for that
    subscription and whose tenant matches the source. Returns (notification, tenant_id)."""
    ids = {n.subscription_id for n in notifications}
    if not ids:
        return []
    rows = (
        await session.execute(
            select(Subscription.graph_subscription_id, Subscription.client_state, Tenant.tenant_id)
            .join(LinkedSource, LinkedSource.id == Subscription.source_id)
            .join(Tenant, Tenant.id == LinkedSource.tenant_pk)
            .where(Subscription.graph_subscription_id.in_(ids))
        )
    ).all()
    known = {sid: (secret, tid) for sid, secret, tid in rows}
    verified = []
    for n in notifications:
        entry = known.get(n.subscription_id)
        if entry is None:
            NOTIFICATIONS_REJECTED.labels("unknown_subscription").inc()
            continue
        secret, tenant_id = entry
        if not n.client_state or not hmac.compare_digest(secret.encode(), n.client_state.encode()):
            NOTIFICATIONS_REJECTED.labels("client_state").inc()
            log.warning("rejected notification with bad clientState for %s", n.subscription_id)
            continue
        if n.tenant_id and tenant_id and n.tenant_id.lower() != tenant_id.lower():
            NOTIFICATIONS_REJECTED.labels("tenant_mismatch").inc()
            continue
        verified.append((n, tenant_id or ""))
    return verified


@router.post("/graph")
async def graph_notifications(
    request: Request,
    validation_token: str | None = Query(default=None, alias="validationToken"),
    services: Services = Depends(get_services),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if validation_token is not None:
        return _validation_response(validation_token)
    started = time.perf_counter()
    for n, tenant_id in await _verified(session, await _parse(request)):
        resource_id = n.resource_id
        if not resource_id:
            NOTIFICATIONS_REJECTED.labels("no_resource").inc()
            continue
        NOTIFICATIONS_RECEIVED.labels(tenant_id).inc()
        # Storms of notifications for one event collapse into one queued fetch.
        await services.queue.enqueue_coalesced(
            notification_key(n.subscription_id, resource_id),
            "process_notification",
            n.subscription_id,
            resource_id,
            n.change_type or "updated",
        )
    WEBHOOK_LATENCY.labels("graph").observe(time.perf_counter() - started)
    return Response(status_code=202)


@router.post("/lifecycle")
async def lifecycle_notifications(
    request: Request,
    validation_token: str | None = Query(default=None, alias="validationToken"),
    services: Services = Depends(get_services),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if validation_token is not None:
        return _validation_response(validation_token)
    started = time.perf_counter()
    for n, tenant_id in await _verified(session, await _parse(request)):
        if not n.lifecycle_event:
            continue
        LIFECYCLE_RECEIVED.labels(tenant_id, n.lifecycle_event).inc()
        log.info("lifecycle %s for subscription %s", n.lifecycle_event, n.subscription_id)
        await services.queue.enqueue_coalesced(
            lifecycle_key(n.subscription_id, n.lifecycle_event),
            "handle_lifecycle",
            n.subscription_id,
            n.lifecycle_event,
        )
    WEBHOOK_LATENCY.labels("lifecycle").observe(time.perf_counter() - started)
    return Response(status_code=202)
