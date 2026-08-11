"""Auditoria e escrita comitam juntas.

Antes, `audit()` abria a própria conexão: a operação comitava e o registro
podia falhar depois, deixando o fato sem rastro (ou o inverso). Passar `conn`
põe os dois na mesma transação.
"""
from __future__ import annotations

import pytest

from api import db
from api.deps import audit

pytestmark = pytest.mark.anyio

ACAO = "teste.transacao"


async def _acoes_registradas() -> list[str]:
    linhas = await db.fetch_all(
        "SELECT entity_id FROM audit_log WHERE action = %s", (ACAO,))
    return [str(linha["entity_id"]) for linha in linhas]


async def test_auditoria_some_com_a_escrita_que_falhou(client):
    """Transação abortada não deixa registro de algo que não aconteceu."""
    async with db.connection() as conn:
        with pytest.raises(RuntimeError):
            async with conn.transaction():
                row = await db.fetch_one(
                    "INSERT INTO project (slug, name, task_type)"
                    " VALUES ('abortado','Abortado','detect') RETURNING id",
                    conn=conn)
                await audit(None, ACAO, "project", str(row["id"]), conn=conn)
                raise RuntimeError("falha depois da escrita e da auditoria")

    assert await _acoes_registradas() == []
    assert await db.fetch_one(
        "SELECT id FROM project WHERE slug = 'abortado'") is None


async def test_auditoria_comita_com_a_escrita_que_deu_certo(client):
    async with db.connection() as conn:
        async with conn.transaction():
            row = await db.fetch_one(
                "INSERT INTO project (slug, name, task_type)"
                " VALUES ('comitado','Comitado','detect') RETURNING id",
                conn=conn)
            await audit(None, ACAO, "project", str(row["id"]), conn=conn)

    assert await _acoes_registradas() == [str(row["id"])]


async def test_helpers_sem_conn_seguem_pegando_conexao_do_pool(client):
    """O parâmetro é opcional: quem não passa nada continua como antes."""
    await db.execute("INSERT INTO project (slug, name, task_type)"
                     " VALUES ('do-pool','Do pool','detect')")
    assert await db.fetch_one(
        "SELECT slug FROM project WHERE slug = 'do-pool'") is not None
