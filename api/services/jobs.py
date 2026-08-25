"""Fila global de jobs: a visão operacional da aba Máquinas.

Cada área acompanha os próprios jobs (treino, export, avaliação, pré-tag);
aqui é o plantão geral — o que espera máquina, o que roda onde, quantas
tentativas — e o cancelar direto da fila, sem caçar a tela que criou o job.

Segue a forma do item 15 do PLANO: toda função que fala com o banco recebe
`conn`, quem abre a conexão é a função pública, e `conn.transaction()` vai no
MESMO `async with` da conexão.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

from .. import db
from ..audit import audit
from ..auth import Principal
from . import imports as svc_imports

#: Desfechos de onde não se sai. É a mesma ideia do `TERMINAIS` do protocolo do
#: agente: cancelar depois do fim não reabre nada.
TERMINAIS = ("succeeded", "failed", "canceled")


async def fila() -> list[dict[str, Any]]:
    """Ativos primeiro (queued/claimed/running), depois os últimos terminados.

    Recolhe as importações órfãs antes de listar, como a tela de jobs do
    projeto: aqui é o plantão geral, e um job preso em `running` para sempre é
    justamente o que se vem procurar.
    """
    await svc_imports.recolher_orfaos()
    async with db.connection() as conn:
        return await db.fetch_all(
            "SELECT j.id, j.type, j.status, j.priority, j.attempts,"
            "       j.max_attempts, j.cancel_requested, j.requires,"
            "       j.created_at, j.started_at, j.finished_at, j.error,"
            "       p.slug AS project, d.name AS device,"
            "       td.name AS target_device"
            "  FROM job j"
            "  JOIN project p ON p.id = j.project_id"
            "  LEFT JOIN device d ON d.id = j.device_id"
            "  LEFT JOIN device td ON td.id = j.target_device_id"
            " ORDER BY (j.status IN ('queued','claimed','running')) DESC,"
            "          j.created_at DESC"
            " LIMIT 100", conn=conn)


async def cancelar(job_id: UUID, admin: Principal,
                   ip: str | None) -> dict[str, Any]:
    """Se ainda na fila, cancela na hora; se já rodando, marca a flag e o
    agente para no próximo heartbeat (mesma semântica do cancelar do treino).

    A guarda de estado terminal vive no `WHERE`, e não numa leitura anterior:
    ler-e-decidir deixa a janela em que o job termina entre as duas coisas, e
    aí o cancelamento marcaria `cancel_requested` num job já encerrado — que é
    exatamente o que o 409 existe para não deixar acontecer.
    """
    async with db.connection() as conn, conn.transaction():
        alvo = await db.fetch_one(
            "UPDATE job SET cancel_requested = true,"
            "  status = CASE WHEN status = 'queued' THEN 'canceled'"
            "                ELSE status END,"
            "  finished_at = CASE WHEN status = 'queued' THEN now()"
            "                     ELSE finished_at END"
            " WHERE id = %s AND status <> ALL(%s::text[])"
            " RETURNING type, status", (job_id, list(TERMINAIS)), conn=conn)
        if alvo is None:
            # Não casou: ou não existe, ou já terminou. A distinção é do
            # usuário — 404 e 409 dizem coisas diferentes.
            j = await db.fetch_one("SELECT status FROM job WHERE id = %s",
                                   (job_id,), conn=conn)
            if j is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    "job não encontrado")
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"job já terminou ({j['status']})")

        # treino cancelado na fila: o run espelha o job (como no cancel da aba
        # Treinamento) — senão fica "queued" para sempre na tela
        if alvo["status"] == "canceled" and alvo["type"] == "train":
            await db.execute(
                "UPDATE training_run SET status = 'stopped',"
                " finished_at = now() WHERE job_id = %s", (job_id,), conn=conn)
        await audit(admin, "job.cancel", "job", str(job_id), ip=ip, conn=conn)
    return {"id": job_id, "status": alvo["status"]}
