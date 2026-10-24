"""Microsoft Graph client for source calendars: delegated tokens per linked source,
per-tenant throttling and a hard read-only guard (FR-11)."""

import logging
import uuid
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from redis.asyncio import Redis

from app.config import Settings
from app.graph.tokens import TokenService
from app.timeutil import isoformat_z, utcnow

log = logging.getLogger(__name__)

DEFAULT_RETRY_AFTER = 10.0
PREFER_UTC = 'outlook.timezone="UTC"'


class GraphError(Exception):
    def __init__(self, status: int, code: str | None, message: str):
        super().__init__(f"Graph {status} {code or ''}: {message}".strip())
        self.status = status
        self.code = code
        self.message = message
        # The linked source whose delegated token made the call.
        self.source_id: uuid.UUID | None = None

    @property
    def transient(self) -> bool:
        return self.status >= 500

    @property
    def auth_failure(self) -> bool:
        """401 even with a freshly refreshed token, or 403: the delegated grant no longer
        lets this user read their calendar (revoked, consent withdrawn, policy change)."""
        return self.status in (401, 403)


class GraphNotFound(GraphError):
    pass


class GraphGone(GraphError):
    """410: delta token expired / resync required."""


class GraphForbidden(GraphError):
    pass


class GraphUnauthorized(GraphError):
    pass


class GraphThrottled(Exception):
    def __init__(self, scope: str, retry_after: float):
        super().__init__(f"Throttled on {scope}; retry after {retry_after:.1f}s")
        self.scope = scope
        self.retry_after = retry_after


class ReadOnlyViolation(RuntimeError):
    """Raised if code ever attempts to write to a source tenant (FR-11)."""


def parse_retry_after(value: str | None) -> float:
    if not value:
        return DEFAULT_RETRY_AFTER
    try:
        return max(float(value), 1.0)
    except ValueError:
        pass
    try:
        return max((parsedate_to_datetime(value) - utcnow()).total_seconds(), 1.0)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER


def _is_subscription_path(path: str) -> bool:
    return path.lstrip("/").lower().startswith("subscriptions")


class GraphClient:
    def __init__(self, http: httpx.AsyncClient, tokens: TokenService, redis: Redis, settings: Settings):
        self.http = http
        self.tokens = tokens
        self.redis = redis
        self.settings = settings

    def source(self, source_id: uuid.UUID, tenant_id: str) -> "SourceGraph":
        return SourceGraph(self, source_id, tenant_id)

    # -- throttling -------------------------------------------------------------------

    async def throttled_for(self, scope: str) -> float:
        """Seconds until `scope` may call Graph again (0 when not throttled)."""
        ttl_ms = await self.redis.pttl(f"throttle:{scope}")
        return ttl_ms / 1000 if ttl_ms and ttl_ms > 0 else 0.0

    async def _throttle(self, scope: str, seconds: float) -> None:
        await self.redis.set(f"throttle:{scope}", "1", px=int(seconds * 1000))

    # -- core request -----------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        source_id: uuid.UUID,
        scope: str,
        json: Any = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        method = method.upper()
        if method != "GET" and not _is_subscription_path(path):
            raise ReadOnlyViolation(f"Refusing {method} {path}: source calendars are read-only")

        wait = await self.throttled_for(scope)
        if wait:
            raise GraphThrottled(scope, wait)

        if path.startswith("http"):
            url = path
        else:
            url = f"{self.settings.graph_base_url.rstrip('/')}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urlencode(params, safe=':$')}"

        for attempt in range(2):
            token = await self.tokens.get_source_token(source_id)
            req_headers = {"Authorization": f"Bearer {token}", **(headers or {})}
            resp = await self.http.request(method, url, json=json, headers=req_headers)
            if resp.status_code == 401 and attempt == 0:
                # Expired or revoked access token: drop it and refresh once.
                await self.tokens.invalidate_source(source_id)
                continue
            break

        if resp.status_code in (429, 503, 504):
            retry_after = parse_retry_after(resp.headers.get("Retry-After"))
            await self._throttle(scope, retry_after)
            log.info(
                "graph throttled scope=%s status=%s retry_after=%.1f", scope, resp.status_code, retry_after
            )
            raise GraphThrottled(scope, retry_after)
        if resp.status_code >= 400:
            code, message = None, resp.text[:500]
            try:
                err = resp.json().get("error", {})
                code, message = err.get("code"), err.get("message", message)
            except ValueError:
                pass
            cls = {
                401: GraphUnauthorized,
                403: GraphForbidden,
                404: GraphNotFound,
                410: GraphGone,
            }.get(resp.status_code, GraphError)
            error = cls(resp.status_code, code, message)
            error.source_id = source_id
            raise error
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()


def _user(user_id: str) -> str:
    return f"users/{quote(user_id, safe='@')}"


def calendar_base(user_id: str, calendar_id: str | None) -> str:
    base = _user(user_id)
    return f"{base}/calendars/{quote(calendar_id, safe='')}" if calendar_id else f"{base}/calendar"


class SourceGraph:
    """Read-only access to one linked source mailbox, with its owner's delegated
    Calendars.Read token. Throttling is still tracked per customer tenant."""

    def __init__(self, client: GraphClient, source_id: uuid.UUID, tenant_id: str):
        self.client = client
        self.source_id = source_id
        self.tenant_id = tenant_id
        self.scope = f"tenant:{tenant_id}"

    async def _call(self, method: str, path: str, **kw: Any) -> dict[str, Any] | None:
        return await self.client.request(method, path, source_id=self.source_id, scope=self.scope, **kw)

    async def get_event(self, user_id: str, event_id: str) -> dict[str, Any]:
        result = await self._call(
            "GET", f"{_user(user_id)}/events/{quote(event_id, safe='')}", headers={"Prefer": PREFER_UTC}
        )
        assert result is not None
        return result

    async def get_default_calendar(self, user_id: str) -> dict[str, Any]:
        result = await self._call("GET", f"{_user(user_id)}/calendar")
        assert result is not None
        return result

    def delta_start_url(self, user_id: str, calendar_id: str | None, start: datetime, end: datetime) -> str:
        base = calendar_base(user_id, calendar_id) if calendar_id else _user(user_id)
        query = urlencode({"startDateTime": isoformat_z(start), "endDateTime": isoformat_z(end)})
        return f"{self.client.settings.graph_base_url.rstrip('/')}/{base}/calendarView/delta?{query}"

    async def delta_page(self, url: str, page_size: int = 100) -> dict[str, Any]:
        result = await self._call(
            "GET", url, headers={"Prefer": f"{PREFER_UTC}, odata.maxpagesize={page_size}"}
        )
        assert result is not None
        return result

    def subscription_resource(self, user_id: str, calendar_id: str | None) -> str:
        if calendar_id:
            return f"users/{user_id}/calendars/{calendar_id}/events"
        return f"users/{user_id}/events"

    async def create_subscription(self, body: dict[str, Any]) -> dict[str, Any]:
        result = await self._call("POST", "subscriptions", json=body)
        assert result is not None
        return result

    async def renew_subscription(self, subscription_id: str, expires: datetime) -> dict[str, Any] | None:
        return await self._call(
            "PATCH", f"subscriptions/{subscription_id}", json={"expirationDateTime": isoformat_z(expires)}
        )

    async def reauthorize_subscription(self, subscription_id: str) -> None:
        await self._call("POST", f"subscriptions/{subscription_id}/reauthorize")

    async def delete_subscription(self, subscription_id: str) -> None:
        await self._call("DELETE", f"subscriptions/{subscription_id}")
