"""Shared runtime dependencies for the API and the workers."""

from dataclasses import dataclass

import httpx
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.crypto import configure_cipher
from app.db import create_engine, create_sessionmaker
from app.graph.client import GraphClient
from app.graph.tokens import TokenService
from app.queue import JobQueue


@dataclass
class Services:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: ArqRedis
    http: httpx.AsyncClient
    tokens: TokenService
    graph: GraphClient
    queue: JobQueue

    @classmethod
    async def create(
        cls,
        settings: Settings,
        *,
        redis: ArqRedis | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        queue: JobQueue | None = None,
    ) -> "Services":
        configure_cipher(settings.encryption_keys)
        engine = create_engine(settings.database_url)
        if redis is None:
            redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        http = httpx.AsyncClient(
            transport=http_transport,
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
        tokens = TokenService(http, redis, settings)
        return cls(
            settings=settings,
            engine=engine,
            sessionmaker=create_sessionmaker(engine),
            redis=redis,
            http=http,
            tokens=tokens,
            graph=GraphClient(http, tokens, redis, settings),
            queue=queue or JobQueue(redis),
        )

    async def close(self) -> None:
        await self.http.aclose()
        await self.redis.aclose()
        await self.engine.dispose()
