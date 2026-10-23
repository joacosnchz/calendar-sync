"""arq entry points.

arq app.worker.settings.WorkerSettings      # horizontally scalable job workers
arq app.worker.settings.SchedulerSettings   # cron sweeps that enqueue work
"""

from typing import Any

from arq import cron, func
from arq.connections import RedisSettings
from prometheus_client import start_http_server

from app.config import get_settings
from app.logconfig import configure_logging
from app.services import Services
from app.worker import jobs, sweeps

_settings = get_settings()
SCHEDULER_QUEUE = "arq:scheduler"


async def _startup(ctx: dict[str, Any]) -> None:
    configure_logging(_settings.log_level)
    ctx["services"] = await Services.create(_settings)


async def _worker_startup(ctx: dict[str, Any]) -> None:
    await _startup(ctx)
    start_http_server(_settings.metrics_port)


async def _shutdown(ctx: dict[str, Any]) -> None:
    services: Services | None = ctx.get("services")
    if services:
        await services.close()


class WorkerSettings:
    functions = [func(f, max_tries=jobs.MAX_TRIES + 5) for f in jobs.JOB_FUNCTIONS]
    on_startup = _worker_startup
    on_shutdown = _shutdown
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    max_jobs = 20
    job_timeout = 900
    keep_result = 300
    health_check_interval = 30


class SchedulerSettings:
    queue_name = SCHEDULER_QUEUE
    functions: list = []
    cron_jobs = [
        cron(sweeps.renew_sweep, minute=set(range(0, 60, 15)), run_at_startup=True),
        cron(sweeps.delta_sweep, minute=set(range(5, 60, 15)), run_at_startup=True),
        cron(sweeps.mirror_audit_sweep, minute={10, 40}),
        cron(sweeps.provisioning_sweep, minute={20, 50}),
    ]
    on_startup = _startup
    on_shutdown = _shutdown
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    health_check_interval = 30
