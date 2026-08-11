"""Pool de conexões psycopg3. SQL direto, sem ORM.

O schema é escrito à mão em db/migrations/; um ORM por cima só adicionaria uma
segunda descrição do mesmo modelo pra manter em sincronia.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import settings

_pool: AsyncConnectionPool | None = None


async def open_pool() -> None:
    global _pool
    _pool = AsyncConnectionPool(settings.database_url, min_size=1, max_size=10,
                               kwargs={"row_factory": dict_row}, open=False)
    await _pool.open(wait=True)


async def close_pool() -> None:
    if _pool is not None:
        await _pool.close()


@asynccontextmanager
async def connection():
    if _pool is None:
        raise RuntimeError("pool não inicializado")
    async with _pool.connection() as conn:
        yield conn


async def fetch_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    async with connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()


async def fetch_all(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    async with connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def execute(sql: str, params: tuple = ()) -> None:
    async with connection() as conn:
        await conn.execute(sql, params)
