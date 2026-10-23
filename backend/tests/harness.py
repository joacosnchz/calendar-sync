"""In-process test harness: API + worker jobs + mock Graph, wired over ASGI transports.

Graph calls from Services are routed to the mock; the mock's webhook deliveries are
routed to the API; jobs the API enqueues are captured and run on demand.
"""

import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from arq import Retry
from arq.connections import ArqRedis
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.api.deps import CSRF_HEADER
from app.api.main import create_app
from app.config import Settings
from app.models import Base, MirrorMapping, SourceEvent, User
from app.queue import JobQueue
from app.services import Services
from app.worker.jobs import JOB_FUNCTIONS
from mockgraph.main import create_mock_app
from mockgraph.state import MockState, MockUser, now_utc, seed

HOME_TENANT = "11111111-1111-4111-8111-111111111111"
SOURCE_APP = "22222222-2222-4222-8222-222222222222"
PRIMARY_APP = "33333333-3333-4333-8333-333333333333"
SHARED_MAILBOX = "calendars@calsync.example"

JOBS = {f.__name__: f for f in JOB_FUNCTIONS}


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        database_url=os.environ.get(
            "TEST_DATABASE_URL", "postgresql+asyncpg://calsync:calsync@localhost:5432/calsync_test"
        ),
        redis_url=os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15"),
        public_api_url="http://api.test",
        public_web_url="http://web.test",
        login_base_url="http://login.test",
        login_browser_base_url=None,
        graph_base_url="http://graph.test/v1.0",
        session_secret="test-session-secret-" + "x" * 40,
        data_encryption_keys=Fernet.generate_key().decode(),
        home_tenant_id=HOME_TENANT,
        source_app_client_id=SOURCE_APP,
        source_app_client_secret="source-secret",
        primary_app_client_id=PRIMARY_APP,
        primary_app_client_secret="primary-secret",
        primary_calendar_mode="shared_mailbox",
        primary_shared_mailbox=SHARED_MAILBOX,
        operator_emails="ops@calsync.example",
    )
    values.update(overrides)
    return Settings(**values)


class HostRouter(httpx.AsyncBaseTransport):
    """Dispatches requests to in-process ASGI apps by hostname."""

    def __init__(self) -> None:
        self.routes: dict[str, httpx.AsyncBaseTransport] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self.routes.get(request.url.host)
        if transport is None:
            raise httpx.ConnectError(f"No route to {request.url.host}")
        return await transport.handle_async_request(request)


class RecordingQueue(JobQueue):
    def __init__(self, redis: ArqRedis):
        super().__init__(redis)
        self.jobs: list[tuple[str, tuple, int]] = []
        self.deferred: list[tuple[str, tuple, int]] = []
        self.history: list[tuple[str, tuple]] = []

    async def enqueue(self, function: str, *args: object, defer_by: float | None = None) -> None:
        self.jobs.append((function, args, 1))
        self.history.append((function, args))

    def names(self) -> list[str]:
        return [j[0] for j in self.jobs]


@dataclass
class Harness:
    settings: Settings
    services: Services
    queue: RecordingQueue
    mock: MockState
    router: HostRouter
    api_transport: httpx.ASGITransport
    mock_transport: httpx.ASGITransport
    results: list[tuple[str, Any]] = field(default_factory=list)

    # -- clients ----------------------------------------------------------------------

    def client(self) -> httpx.AsyncClient:
        """A browser: cookie jar + the CSRF header the web app sends."""
        return httpx.AsyncClient(
            transport=self.router,
            base_url="http://web.test",
            headers={CSRF_HEADER: "1"},
            follow_redirects=False,
        )

    def mock_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.mock_transport, base_url="http://login.test")

    # -- jobs -------------------------------------------------------------------------

    async def run_jobs(self, limit: int = 2000) -> list[tuple[str, Any]]:
        ran: list[tuple[str, Any]] = []
        while self.queue.jobs:
            if len(ran) >= limit:
                raise RuntimeError(f"Job loop did not settle: {self.queue.names()[:20]}")
            name, args, job_try = self.queue.jobs.pop(0)
            ctx = {"services": self.services, "job_try": job_try}
            try:
                result = await JOBS[name](ctx, *args)
            except Retry:
                self.queue.deferred.append((name, args, job_try + 1))
                result = "deferred"
            ran.append((name, result))
            self.results.append((name, result))
        return ran

    async def release_deferred(self) -> None:
        keys = await self.services.redis.keys("throttle:*")
        if keys:
            await self.services.redis.delete(*keys)
        self.queue.jobs.extend(self.queue.deferred)
        self.queue.deferred.clear()

    # -- flows ------------------------------------------------------------------------

    async def _oauth_roundtrip(self, browser: httpx.AsyncClient, start_path: str, upn: str) -> httpx.Response:
        start = await browser.get(start_path)
        assert start.status_code == 302, start.text
        query = {k: v[0] for k, v in parse_qs(urlparse(start.headers["location"]).query).items()}
        user = self.mock.user_by_upn(upn)
        async with self.mock_client() as mock:
            authorized = await mock.get("/mock/authorize/complete", params={**query, "oid": user.oid})
        assert authorized.status_code == 302
        return await browser.get(authorized.headers["location"])

    async def sign_in(self, upn: str) -> httpx.AsyncClient:
        browser = self.client()
        resp = await self._oauth_roundtrip(browser, "/auth/login", upn)
        assert resp.status_code == 302 and "cs_session" in resp.headers.get("set-cookie", ""), resp.text
        return browser

    async def link(self, browser: httpx.AsyncClient, upn: str) -> dict[str, list[str]]:
        resp = await self._oauth_roundtrip(browser, "/auth/link", upn)
        assert resp.status_code == 307 or resp.status_code == 302, resp.text
        return parse_qs(urlparse(resp.headers["location"]).query)

    async def onboard_tenant(self, operator: httpx.AsyncClient, domain: str, *, approve: bool = True) -> dict:
        tenant = self.mock.find_tenant(domain)
        assert tenant
        created = await operator.post(
            "/api/admin/tenants", json={"display_name": tenant.name, "domain": domain}
        )
        assert created.status_code == 201, created.text
        return await self.consent(created.json()["consent_url"], tenant.tenant_id, approve=approve)

    async def consent(self, consent_url: str, tenant_id: str, *, approve: bool = True) -> dict:
        query = {k: v[0] for k, v in parse_qs(urlparse(consent_url).query).items()}
        async with self.mock_client() as mock:
            resp = await mock.post(
                "/mock/adminconsent/complete",
                data={**query, "tenant_id": tenant_id, "decision": "approve" if approve else "deny"},
            )
        assert resp.status_code == 303
        async with self.client() as browser:
            callback = await browser.get(resp.headers["location"])
        assert callback.status_code in (302, 307)
        return {k: v[0] for k, v in parse_qs(urlparse(callback.headers["location"]).query).items()}

    # -- inspection -------------------------------------------------------------------

    def primary_events(self) -> list[dict[str, Any]]:
        shared = self.mock.user_by_upn(SHARED_MAILBOX)
        return [
            ev
            for cal in self.mock.calendars.values()
            if cal.owner_oid == shared.oid and cal.id != shared.default_calendar_id
            for ev in cal.events.values()
        ]

    def primary_subjects(self) -> list[str]:
        return sorted(ev["subject"] for ev in self.primary_events())

    async def user(self, email: str) -> User:
        async with self.services.sessionmaker() as session:
            user = await session.scalar(select(User).where(User.email == email))
            assert user
            return user

    async def source_event_count(self) -> int:
        async with self.services.sessionmaker() as session:
            return len((await session.scalars(select(SourceEvent.id))).all())

    async def mapping_count(self) -> int:
        async with self.services.sessionmaker() as session:
            return len((await session.scalars(select(MirrorMapping.id))).all())

    async def create_source_event(
        self, upn: str, subject: str, *, hours_from_now: float = 24, **extra: Any
    ) -> dict:
        start = (now_utc() + timedelta(hours=hours_from_now)).replace(minute=0, second=0, microsecond=0)
        payload = {
            "subject": subject,
            "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
            "end": {
                "dateTime": (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "UTC",
            },
            **extra,
        }
        ical_uid = payload.pop("ical_uid", None)
        return await self.mock.create_event(self.mock.user_by_upn(upn), payload, ical_uid=ical_uid)


async def build_harness(settings: Settings, redis: ArqRedis) -> Harness:
    router = HostRouter()
    queue = RecordingQueue(redis)
    services = await Services.create(settings, redis=redis, http_transport=router, queue=queue)
    mock = MockState(
        home_tenant_id=HOME_TENANT,
        source_app_id=SOURCE_APP,
        source_app_secret="source-secret",
        primary_app_id=PRIMARY_APP,
        primary_app_secret="primary-secret",
        shared_mailbox=SHARED_MAILBOX,
        inline_delivery=True,
        notify_transport=router,
    )
    seed(mock)
    api_transport = httpx.ASGITransport(app=create_app(services))
    mock_transport = httpx.ASGITransport(app=create_mock_app(mock, seed_demo=False))
    router.routes.update(
        {
            "web.test": api_transport,
            "api.test": api_transport,
            "login.test": mock_transport,
            "graph.test": mock_transport,
        }
    )
    async with services.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    return Harness(settings, services, queue, mock, router, api_transport, mock_transport)


def tenant_of(mock: MockState, upn: str) -> str:
    user: MockUser = mock.user_by_upn(upn)
    return user.tenant_id
