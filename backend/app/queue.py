"""Job queue on Redis (arq), with coalescing for notification storms.

Coalescing: a job is enqueued only if no identical job is still *waiting*. The job
clears its pending marker right before it reads from Graph, so any change that lands
after that read enqueues a fresh job. Duplicates collapse; updates are never lost.
"""

import hashlib
import uuid

from arq.connections import ArqRedis

PENDING_TTL_SECONDS = 600


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def notification_key(subscription_id: str, resource_id: str) -> str:
    return f"notify:{subscription_id}:{_h(resource_id)}"


def sync_key(source_id: uuid.UUID | str, mode: str) -> str:
    return f"sync:{source_id}:{mode}"


def lifecycle_key(subscription_id: str, event: str) -> str:
    return f"lifecycle:{subscription_id}:{event}"


def renew_key(subscription_pk: uuid.UUID | str) -> str:
    return f"renew:{subscription_pk}"


class JobQueue:
    def __init__(self, redis: ArqRedis):
        self.redis = redis

    async def enqueue(self, function: str, *args: object, defer_by: float | None = None) -> None:
        await self.redis.enqueue_job(function, *args, _defer_by=defer_by)

    async def enqueue_coalesced(self, key: str, function: str, *args: object) -> bool:
        if await self.redis.set(f"pending:{key}", "1", nx=True, ex=PENDING_TTL_SECONDS):
            await self.enqueue(function, *args)
            return True
        return False

    async def release(self, key: str) -> None:
        await self.redis.delete(f"pending:{key}")
