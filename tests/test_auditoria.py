"""Auditoria e escrita comitam juntas.

Antes, `audit()` abria a própria conexão: a operação comitava e o registro
podia falhar depois, deixando o fato sem rastro (ou o inverso). Passar `conn`
põe os dois na mesma transação.
"""
from __future__ import annotations

import pytest

from api import db
from api.audit import audit

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


async def test_aceitar_convite_audita_dentro_da_transacao(admin, client):
    """Aceitar convite é a única escrita feita antes de existir sessão: o
    registro tem de comitar junto com a senha, não à parte."""
    novo = await admin.post("/v1/users", json={"email": "auditado@teste.com",
                                               "display_name": "Auditado"})
    uid = novo.json()["id"]
    token = (await admin.post(f"/v1/users/{uid}/invite")).json()["token"]

    r = await client.post("/v1/invite/accept",
                          json={"token": token, "password": "senha-123456"})
    assert r.status_code == 200

    linha = await db.fetch_one(
        "SELECT actor_type, entity_id, note FROM audit_log"
        " WHERE action = 'user.invite.accept' AND entity_id = %s", (uid,))
    assert linha is not None
    assert linha["actor_type"] == "system"   # ainda não há principal
    assert linha["note"] == "via convite"


async def test_convite_recusado_nao_deixa_registro(client):
    await client.post("/v1/invite/accept",
                      json={"token": "nao-existe", "password": "senha-123456"})
    assert await db.fetch_all(
        "SELECT id FROM audit_log WHERE action = 'user.invite.accept'") == []


async def test_helpers_sem_conn_seguem_pegando_conexao_do_pool(client):
    """O parâmetro é opcional: quem não passa nada continua como antes."""
    await db.execute("INSERT INTO project (slug, name, task_type)"
                     " VALUES ('do-pool','Do pool','detect')")
    assert await db.fetch_one(
        "SELECT slug FROM project WHERE slug = 'do-pool'") is not None
