"""
Database connection pool.

Created ONCE at application startup (see main.py's lifespan handler),
reused for every request. Never open a new connection per-request, and
never run schema migrations here -- migrations are an explicit deploy
step (e.g. `alembic upgrade head` or `python database/migrate.py`),
separate from starting the API.
"""

from typing import Optional

import asyncpg

from app.core.config import settings

_pool: Optional[asyncpg.Pool] = None


async def connect_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )


async def disconnect_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised - is the app lifespan running?")
    return _pool
