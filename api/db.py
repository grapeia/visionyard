"""Pool de conexões psycopg3. SQL direto, sem ORM.

O schema é escrito à mão em db/migrations/; um ORM por cima só adicionaria uma
segunda descrição do mesmo modelo pra manter em sincronia.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import settings

# O parâmetro de `AsyncConnection` é o tipo de linha, fixado em `dict_row` na
# criação do pool. Sem esta anotação o `conn` do `async with connection()` sai
# como `Any` e o `mypy` deixa passar `(await cur.fetchone())["x"]` — que é
# exatamente o `TypeError` que o item 9 do PLANO foi fechar.
Conn = AsyncConnection[dict[str, Any]]

_pool: AsyncConnectionPool[Conn] | None = None

# Parâmetros de query: tupla para `%s` posicional, dict para `%(nome)s` — as
# duas formas estão em uso (a nomeada onde o mesmo valor repete na query).
Params = Sequence[Any] | Mapping[str, Any]


async def open_pool() -> None:
    global _pool
    _pool = AsyncConnectionPool[Conn](
        settings.database_url, min_size=1, max_size=10,
        kwargs={"row_factory": dict_row}, open=False)
    await _pool.open(wait=True)


async def close_pool() -> None:
    if _pool is not None:
        await _pool.close()


@asynccontextmanager
async def connection() -> AsyncIterator[Conn]:
    if _pool is None:
        raise RuntimeError("pool não inicializado")
    async with _pool.connection() as conn:
        yield conn


@asynccontextmanager
async def _usar(conn: Conn | None) -> AsyncIterator[Conn]:
    """Reaproveita a conexão recebida, ou pega uma do pool quando não há.

    É o que permite uma escrita e a sua auditoria caírem na mesma transação:
    sem `conn`, cada helper abre a sua própria conexão e o `audit()` comita à
    parte — se ele falhar, o fato fica sem registro (e vice-versa).
    """
    if conn is not None:
        yield conn
    else:
        async with connection() as nova:
            yield nova


async def fetch_one(sql: str, params: Params = (), *,
                    conn: Conn | None = None) -> dict[str, Any] | None:
    async with _usar(conn) as c:
        cur = await c.execute(sql, params)
        return await cur.fetchone()


async def fetch_row(sql: str, params: Params = (), *,
                    conn: Conn | None = None) -> dict[str, Any]:
    """Como `fetch_one`, mas para query que SEMPRE devolve linha: `INSERT ...
    RETURNING`, `count(*)`, agregado sem `GROUP BY`.

    Serve para distinguir os dois motivos de indexar o retorno sem checar
    `None`. Aqui a ausência de linha é bug de programação, não entrada do
    usuário — o que se quer é estourar na query, com o SQL à vista, em vez de um
    `TypeError: 'NoneType' object is not subscriptable` algumas linhas adiante.
    Onde a linha PODE faltar (o registro não existe), o certo continua sendo
    `fetch_one` + 404.
    """
    row = await fetch_one(sql, params, conn=conn)
    if row is None:
        raise RuntimeError(
            f"query que sempre devolve linha veio vazia: {' '.join(sql.split())[:160]}")
    return row


async def fetch_all(sql: str, params: Params = (), *,
                    conn: Conn | None = None) -> list[dict[str, Any]]:
    async with _usar(conn) as c:
        cur = await c.execute(sql, params)
        return await cur.fetchall()


async def execute(sql: str, params: Params = (), *,
                  conn: Conn | None = None) -> None:
    async with _usar(conn) as c:
        await c.execute(sql, params)
