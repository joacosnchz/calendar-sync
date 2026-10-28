"""End-to-end smoke test against a running stack (docker compose, mock profile).

Drives the real services the way a browser would: OAuth redirects and cookies (each
user consents to delegated Calendars.Read for their own mailbox; no admin consent), then
changes in the mock Graph control panel, and checks each change reaches the user's
unified calendar (GET /api/calendar). Also revokes a user's refresh tokens and
reconnects the source.
Needs a fresh stack (it signs users up and links mailboxes). Run from the host network
so localhost URLs resolve:

    docker compose down -v && docker compose up -d --build
    docker run --rm --network host calendar-sync-backend:local python scripts/e2e_smoke.py
"""

import argparse
import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx

CSRF = {"x-calsync-csrf": "1"}


class Smoke:
    def __init__(self, web: str, mock: str):
        self.web = web.rstrip("/")
        self.mock = mock.rstrip("/")
        self.failures = 0
        # The signed-in user whose unified calendar the checks watch.
        self.viewer: httpx.AsyncClient | None = None

    def browser(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(follow_redirects=False, timeout=15, headers=CSRF)

    async def follow(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        for _ in range(10):
            resp = await client.get(url)
            if resp.status_code not in (302, 303, 307):
                return resp
            url = resp.headers["location"]
        raise RuntimeError("redirect loop")

    async def oauth(self, client: httpx.AsyncClient, start: str, upn: str) -> str:
        """Run a sign-in round trip and return the final URL the browser lands on."""
        resp = await client.get(f"{self.web}{start}")
        assert resp.status_code in (302, 307), resp.text
        authorize = urlparse(resp.headers["location"])
        query = {k: v[0] for k, v in parse_qs(authorize.query).items()}
        tenant_path = authorize.path.strip("/").split("/")[0]  # organizations or a tenant id
        oid = next(u["oid"] for u in await self.users() if u["upn"] == upn)
        resp = await client.get(
            f"{self.mock}/mock/authorize/complete", params={**query, "oid": oid, "tenant_path": tenant_path}
        )
        resp = await client.get(resp.headers["location"])  # our /auth/callback
        return resp.headers.get("location", "")

    async def users(self) -> list[dict]:
        async with httpx.AsyncClient() as c:
            state = (await c.get(f"{self.mock}/mock/state")).json()
        return [{"upn": cal["owner"], "oid": cal["owner_oid"]} for cal in state["calendars"].values()]

    async def state(self) -> dict:
        async with httpx.AsyncClient() as c:
            return (await c.get(f"{self.mock}/mock/state")).json()

    async def calendar(self) -> list[dict]:
        assert self.viewer is not None
        now = datetime.now(UTC)
        resp = await self.viewer.get(
            f"{self.web}/api/calendar",
            params={
                "start": (now - timedelta(days=1)).isoformat(),
                "end": (now + timedelta(days=60)).isoformat(),
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def check(self, name: str, fn: Callable[[], Awaitable[str | None]]) -> None:
        started = time.perf_counter()
        try:
            detail = await fn()
            print(f"  PASS  {name} ({time.perf_counter() - started:.1f}s){f' — {detail}' if detail else ''}")
        except Exception as exc:  # noqa: BLE001 - report and keep going
            self.failures += 1
            print(f"  FAIL  {name}: {exc!r}")

    async def wait_for(self, predicate: Callable[[list[dict]], bool], timeout: float = 20) -> float:
        started = time.perf_counter()
        while time.perf_counter() - started < timeout:
            if predicate(await self.calendar()):
                return time.perf_counter() - started
            await asyncio.sleep(0.2)
        raise TimeoutError(
            f"condition not met within {timeout}s; calendar={[e['subject'] for e in await self.calendar()]}"
        )


def day(offset: int) -> str:
    """A date inside the sync window."""
    return (datetime.now(UTC) + timedelta(days=offset)).strftime("%Y-%m-%d")


def subjects(events: list[dict]) -> set[str]:
    return {e["subject"] for e in events}


async def main(web: str, mock: str) -> int:
    s = Smoke(web, mock)
    state = await s.state()
    tenants = {v["name"]: k for k, v in state["tenants"].items()}
    print(f"Smoke test against {web} (mock Graph at {mock})")

    operator, alice = s.browser(), s.browser()
    s.viewer = alice

    async def operator_signs_in() -> str:
        landed = await s.oauth(operator, "/auth/login", "ops@calsync.example")
        me = (await operator.get(f"{web}/api/me")).json()
        assert me["is_operator"], me
        return landed

    async def alice_signs_in_and_links() -> str:
        landed = await s.oauth(alice, "/auth/login", "alice@contoso.example")
        assert landed.endswith("/calendar"), landed
        for upn in ("alice@contoso.example", "alice@fabrikam.example"):
            landed = await s.oauth(alice, "/auth/link", upn)
            assert "status=linked" in landed, landed
        return "2 sources linked, each with the user's own consent"

    async def admin_approval_required() -> str:
        northwind = tenants["Northwind Traders"]
        async with httpx.AsyncClient() as c:
            await c.post(f"{mock}/mock/tenants/{northwind}", data={"action": "consent_block"})
            try:
                landed = await s.oauth(alice, "/auth/link", "alice@northwind.example")
            finally:
                await c.post(f"{mock}/mock/tenants/{northwind}", data={"action": "consent_block"})
        assert "status=admin_approval_required" in landed, landed
        return "tenant that blocks user consent is reported, nothing linked"

    async def source_status(email: str) -> dict:
        sources = (await alice.get(f"{web}/api/sources")).json()
        return next(x for x in sources if x["source_email"] == email)

    async def backfill() -> str:
        took = await s.wait_for(
            lambda e: (
                {"Contoso weekly sync", "Fabrikam board meeting", "Contoso x Fabrikam partnership"}
                <= subjects(e)
            )
        )
        events = await s.calendar()
        assert all("attendees" not in e for e in events), "an event has attendees"
        shared = [e for e in events if "partnership" in e["subject"]]
        assert len(shared) == 1, "duplicate meeting shown twice"
        assert shared[0]["labels"] == ["Contoso", "Fabrikam"], shared
        return f"{len(events)} events in {took:.1f}s, none with attendees, shared meeting shown once"

    async def create_event() -> str:
        async with httpx.AsyncClient() as c:
            await c.post(
                f"{mock}/mock/events",
                data={
                    "mailbox_upn": "alice@contoso.example",
                    "subject": "Smoke: new meeting",
                    "date": day(2),
                    "time": "09:00",
                    "tz": "Pacific Standard Time",
                    "minutes": "45",
                },
            )
        took = await s.wait_for(lambda e: "Smoke: new meeting" in subjects(e))
        event = next(e for e in await s.calendar() if e["subject"] == "Smoke: new meeting")
        expected = datetime.fromisoformat(f"{day(2)}T09:00:00").replace(
            tzinfo=ZoneInfo("America/Los_Angeles")
        )
        assert datetime.fromisoformat(event["start"]) == expected, event
        assert event["labels"] == ["Contoso"], event
        return f"change-to-calendar latency {took:.2f}s, time zone converted"

    async def find_source_event(subject: str) -> tuple[str, str]:
        st = await s.state()
        for cal in st["calendars"].values():
            for e in cal["events"]:
                if e["subject"] == subject:
                    return cal["owner_oid"], e["id"]
        raise LookupError(subject)

    async def update_event() -> str:
        oid, eid = await find_source_event("Smoke: new meeting")
        async with httpx.AsyncClient() as c:
            await c.post(f"{mock}/mock/events/{oid}/{eid}", data={"action": "rename"})
        took = await s.wait_for(lambda e: "Smoke: new meeting (updated)" in subjects(e))
        return f"{took:.2f}s"

    async def delete_event() -> str:
        oid, eid = await find_source_event("Smoke: new meeting (updated)")
        async with httpx.AsyncClient() as c:
            await c.post(f"{mock}/mock/events/{oid}/{eid}", data={"action": "delete"})
        took = await s.wait_for(lambda e: not any("Smoke: new meeting" in x for x in subjects(e)))
        return f"{took:.2f}s"

    async def missed_notifications() -> str:
        contoso = tenants["Contoso"]
        async with httpx.AsyncClient() as c:
            await c.post(f"{mock}/mock/tenants/{contoso}", data={"action": "drop"})
            await c.post(
                f"{mock}/mock/events",
                data={"mailbox_upn": "alice@contoso.example", "subject": "Smoke: dropped", "date": day(3)},
            )
            await asyncio.sleep(2)
            assert "Smoke: dropped" not in subjects(await s.calendar()), "notification was not dropped"
            await c.post(f"{mock}/mock/tenants/{contoso}", data={"action": "drop"})
            await c.post(f"{mock}/mock/tenants/{contoso}", data={"action": "missed"})
        took = await s.wait_for(lambda e: "Smoke: dropped" in subjects(e))
        return f"recovered by delta {took:.2f}s after 'missed'"

    async def throttled() -> str:
        fabrikam = tenants["Fabrikam"]
        async with httpx.AsyncClient() as c:
            await c.post(f"{mock}/mock/tenants/{fabrikam}", data={"action": "throttle"})
            await c.post(
                f"{mock}/mock/events",
                data={"mailbox_upn": "alice@fabrikam.example", "subject": "Smoke: throttled", "date": day(4)},
            )
        took = await s.wait_for(lambda e: "Smoke: throttled" in subjects(e), timeout=60)
        return f"arrived after backoff in {took:.1f}s"

    async def subscription_removed() -> str:
        before = {x["id"] for x in (await s.state())["subscriptions"]}
        async with httpx.AsyncClient() as c:
            await c.post(f"{mock}/mock/tenants/{tenants['Fabrikam']}", data={"action": "subscriptionRemoved"})
        deadline = time.perf_counter() + 20
        while time.perf_counter() < deadline:
            after = {x["id"] for x in (await s.state())["subscriptions"]}
            if len(after) == len(before) and after != before:
                return "recreated"
            await asyncio.sleep(0.3)
        raise TimeoutError("subscription was not recreated")

    async def revoke() -> str:
        async with httpx.AsyncClient() as c:
            await c.post(f"{mock}/mock/users/alice@contoso.example/revoke")
        deadline = time.perf_counter() + 30
        while (await source_status("alice@contoso.example"))["status"] != "reauth_required":
            if time.perf_counter() > deadline:
                raise TimeoutError(await source_status("alice@contoso.example"))
            await asyncio.sleep(0.3)
        source = await source_status("alice@contoso.example")
        assert source["reauth_url"] == f"/auth/reauth/{source['id']}", source
        remaining = subjects(await s.calendar())
        assert "Contoso weekly sync" in remaining, remaining
        return f"paused with events kept: {source['last_error']}"

    async def reconnect() -> str:
        async with httpx.AsyncClient() as c:
            await c.post(
                f"{mock}/mock/events",
                data={
                    "mailbox_upn": "alice@contoso.example",
                    "subject": "Smoke: while paused",
                    "date": day(5),
                },
            )
        await asyncio.sleep(1)
        assert "Smoke: while paused" not in subjects(await s.calendar()), "synced while paused"
        source = await source_status("alice@contoso.example")
        wrong = await s.oauth(alice, source["reauth_url"], "bob@contoso.example")
        assert "status=error" in wrong, wrong
        landed = await s.oauth(alice, source["reauth_url"], "alice@contoso.example")
        assert "status=reconnected" in landed, landed
        took = await s.wait_for(lambda e: "Smoke: while paused" in subjects(e), timeout=30)
        assert (await source_status("alice@contoso.example"))["status"] == "active"
        return f"wrong account rejected; change made while paused shown {took:.1f}s after reconnect"

    async def unlink() -> str:
        sources = (await alice.get(f"{web}/api/sources")).json()
        for source in sources:
            assert (await alice.delete(f"{web}/api/sources/{source['id']}")).status_code == 202
        took = await s.wait_for(lambda e: not e)
        return f"all events removed in {took:.1f}s"

    async def health() -> str:
        overview = (await operator.get(f"{web}/api/admin/overview")).json()
        return (
            f"tenants={overview['tenants']} sources={overview['sources']} events={overview['events']} "
            f"errors_24h={overview['errors_24h']}"
        )

    await s.check("operator signs in", operator_signs_in)
    await s.check("Alice signs in and links two tenants (user consent)", alice_signs_in_and_links)
    await s.check("blocked user consent needs admin approval", admin_approval_required)
    await s.check("initial backfill shown", backfill)
    await s.check("created event is shown", create_event)
    await s.check("updated event is shown", update_event)
    await s.check("deleted event is removed", delete_event)
    await s.check("missed notifications recovered", missed_notifications)
    await s.check("throttled tenant recovers", throttled)
    await s.check("removed subscription is recreated", subscription_removed)
    await s.check("revoked refresh token pauses the source", revoke)
    await s.check("reconnecting resumes and catches up", reconnect)
    await s.check("unlinking removes all events", unlink)
    await s.check("operator health overview", health)

    await operator.aclose()
    await alice.aclose()
    print("OK" if not s.failures else f"{s.failures} check(s) failed")
    return 1 if s.failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--web", default="http://localhost:3000")
    parser.add_argument("--mock", default="http://localhost:8001")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.web, args.mock)))
