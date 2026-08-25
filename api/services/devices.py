"""Cadastro das máquinas que executam jobs (agentes de treino e inferência).

Device é infraestrutura da instalação, não de um projeto — os servidores de GPU
e os dispositivos de borda servem a todos os projetos. Por isso só o
superusuário gerencia, e o RBAC por projeto não tem o que dizer aqui.

`capabilities` é o que decide o roteamento: um job declara o que precisa
(`requires`, ex. `{"train": true}`) e o claim só entrega a device cujo
`capabilities` contém aquilo. Marcar o dispositivo de produção como `draining`
o tira do pool sem apagá-lo — ele continua servindo a linha, mas não pega
pré-tag.

Segue a forma do item 15 do PLANO: toda função que fala com o banco recebe
`conn`, quem abre a conexão é a função pública, e `conn.transaction()` vai no
MESMO `async with` da conexão.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, status

from .. import db
from ..audit import audit
from ..auth import Principal
from ..security import new_token, token_digest


async def _maquina_ou_404(conn: db.Conn, slug: str,
                          colunas: str = "id") -> dict[str, Any]:
    """`colunas` é SQL literal escrito aqui — nunca valor de requisição."""
    row = await db.fetch_one(f"SELECT {colunas} FROM device WHERE slug = %s",
                             (slug,), conn=conn)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device não encontrado")
    return row


async def listar() -> list[dict[str, Any]]:
    async with db.connection() as conn:
        return await db.fetch_all(
            "SELECT id, slug, name, device_class, capabilities, labels,"
            "       status, last_heartbeat, registered_at,"
            "       (SELECT count(*) FROM job j WHERE j.device_id = device.id"
            "         AND j.status = 'running') AS rodando"
            "  FROM device ORDER BY slug", conn=conn)


async def listar_para_implantacao() -> list[dict[str, Any]]:
    """Só nome/classe/estado: esta lista é visível a qualquer autenticado,
    porque o engenheiro escolhe o alvo ao implantar. Capabilities e token
    ficam com o superusuário."""
    async with db.connection() as conn:
        return await db.fetch_all(
            "SELECT slug, name, device_class, status FROM device"
            " WHERE status <> 'disabled' ORDER BY slug", conn=conn)


async def cadastrar(admin: Principal, *, slug: str, name: str,
                    device_class: str, capabilities: dict, labels: list[str],
                    ip: str | None) -> dict[str, Any]:
    """Cadastra e devolve o token em claro — a única vez que ele existe."""
    token = new_token()
    async with db.connection() as conn, conn.transaction():
        # `ON CONFLICT` e não ler-antes-de-inserir: em READ COMMITTED dois
        # cadastros simultâneos do mesmo slug leem "não existe" os dois, e o
        # segundo estoura no UNIQUE de `device.slug` — 500 no lugar do 409.
        # Quem decide é o índice, que é o único que enxerga as duas transações.
        row = await db.fetch_one(
            "INSERT INTO device (slug, name, device_class, capabilities,"
            " labels, token_sha256, status, registered_by)"
            " VALUES (%s,%s,%s,%s::jsonb,%s,%s,'offline',%s)"
            " ON CONFLICT (slug) DO NOTHING"
            " RETURNING id, slug, name, device_class, capabilities, labels,"
            "           status",
            (slug, name, device_class, json.dumps(capabilities), labels,
             token_digest(token), admin.id), conn=conn)
        if row is None:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"já existe device '{slug}'")
        await audit(admin, "device.create", "device", str(row["id"]),
                    after={"slug": slug, "class": device_class}, ip=ip,
                    conn=conn)
    return {**row, "token": token}


async def rotacionar_token(slug: str, admin: Principal,
                           ip: str | None) -> dict[str, Any]:
    token = new_token()
    async with db.connection() as conn, conn.transaction():
        # Um comando só: ler o id para depois atualizar por ele deixaria uma
        # janela entre as duas coisas sem nada ganho em troca.
        row = await db.fetch_one(
            "UPDATE device SET token_sha256 = %s WHERE slug = %s"
            " RETURNING id", (token_digest(token), slug), conn=conn)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "device não encontrado")
        await audit(admin, "device.token.rotate", "device", str(row["id"]),
                    ip=ip, conn=conn)
    return {"slug": slug, "token": token}


async def excluir(slug: str, admin: Principal, ip: str | None) -> None:
    """Exclui uma máquina. Recusa se ela tem histórico de jobs.

    `job.device_id` guarda quem EXECUTOU cada treino — apagar a máquina apagaria
    esse rastro (e o FK bloquearia). Para retirar uma máquina em uso, o caminho
    é `disabled` (mantém o histórico e invalida o token); excluir é só para uma
    máquina cadastrada por engano, que nunca rodou nada.
    """
    async with db.connection() as conn, conn.transaction():
        dev = await _maquina_ou_404(conn, slug)
        # A condição vai DENTRO do DELETE: contar num comando e apagar noutro
        # deixa a janela em que a máquina reclama um job entre os dois, e aí
        # quem recusaria seria o FK — com 500 em vez do 409 que explica o que
        # fazer.
        apagada = await db.fetch_one(
            "DELETE FROM device WHERE id = %(id)s"
            "   AND NOT EXISTS (SELECT 1 FROM job WHERE device_id = %(id)s)"
            " RETURNING id", {"id": dev["id"]}, conn=conn)
        if apagada is None:
            n = await db.fetch_row(
                "SELECT count(*) AS n FROM job WHERE device_id = %s",
                (dev["id"],), conn=conn)
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"máquina tem {n['n']} job(s) no histórico — desative em vez "
                f"de excluir, para preservar quem executou o quê")
        await audit(admin, "device.delete", "device", str(dev["id"]),
                    before={"slug": slug}, ip=ip, conn=conn)


async def definir_estado(slug: str, estado: str, admin: Principal,
                         ip: str | None) -> dict[str, Any]:
    """`draining` tira do pool sem apagar; `disabled` também invalida o token.
    `busy`/`offline` são do agente (heartbeat), não se define à mão."""
    async with db.connection() as conn, conn.transaction():
        row = await db.fetch_one(
            "UPDATE device SET status = %s WHERE slug = %s"
            " RETURNING id, slug, status", (estado, slug), conn=conn)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "device não encontrado")
        await audit(admin, "device.status", "device", str(row["id"]),
                    after={"status": estado}, ip=ip, conn=conn)
    return row
