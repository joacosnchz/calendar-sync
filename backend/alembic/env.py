import asyncio
import os

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.models import Base

target_metadata = Base.metadata


def _url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql+asyncpg://calsync:calsync@db:5432/calsync")


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
