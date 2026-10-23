from collections.abc import AsyncIterator

import pytest_asyncio
from arq import create_pool
from arq.connections import RedisSettings

from tests.harness import Harness, build_harness, make_settings


@pytest_asyncio.fixture
async def harness() -> AsyncIterator[Harness]:
    settings = make_settings()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    await redis.flushdb()
    h = await build_harness(settings, redis)
    try:
        yield h
    finally:
        await h.services.close()


@pytest_asyncio.fixture
async def operator(harness: Harness):
    browser = await harness.sign_in("ops@calsync.example")
    await harness.run_jobs()
    yield browser
    await browser.aclose()


@pytest_asyncio.fixture
async def alice(harness: Harness):
    """Alice signed in, with a ready primary calendar."""
    browser = await harness.sign_in("alice@contoso.example")
    await harness.run_jobs()
    yield browser
    await browser.aclose()
