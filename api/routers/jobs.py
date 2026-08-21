"""Fila global de jobs: a visão operacional da aba Máquinas.

Cada área acompanha os próprios jobs (treino, export, avaliação, pré-tag);
aqui é o plantão geral — o que espera máquina, o que roda onde, quantas
tentativas — e o cancelar direto da fila, sem caçar a tela que criou o job.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel

from .. import auth, db
from ..audit import audit
from ..auth import Principal
from ..deps import require_superuser
from ..services import imports as svc_imports

router = APIRouter(tags=["jobs"])


class JobDaFila(BaseModel):
    """A linha do plantão. `device` é quem executa; `target_device` é a máquina
    em que o job foi fixado (nulo = qualquer uma do pool)."""
    id: UUID
    type: str
    status: str
    priority: int
    attempts: int
    max_attempts: int
    cancel_requested: bool
    requires: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    project: str
    device: str | None
    target_device: str | None


class JobCancelado(BaseModel):
    id: UUID
    status: str


@router.get("/jobs", response_model=list[JobDaFila],
            dependencies=[Depends(require_superuser)])
async def fila():
    """Ativos primeiro (queued/claimed/running), depois os últimos terminados.

    `device` é quem está executando; `target_device` é a máquina em que o job
    foi fixado (NULL = qualquer uma do pool que cumpra os requisitos).

    Recolhe as importações órfãs antes de listar, como a tela de jobs do
    projeto: aqui é o plantão geral, e um job preso em `running` para sempre é
    justamente o que se vem procurar."""
    await svc_imports.recolher_orfaos()
    return await db.fetch_all(
        "SELECT j.id, j.type, j.status, j.priority, j.attempts, j.max_attempts,"
        "       j.cancel_requested, j.requires, j.created_at, j.started_at,"
        "       j.finished_at, j.error,"
        "       p.slug AS project, d.name AS device, td.name AS target_device"
        "  FROM job j"
        "  JOIN project p ON p.id = j.project_id"
        "  LEFT JOIN device d ON d.id = j.device_id"
        "  LEFT JOIN device td ON td.id = j.target_device_id"
        " ORDER BY (j.status IN ('queued','claimed','running')) DESC,"
        "          j.created_at DESC"
        " LIMIT 100")


@router.post("/jobs/{job_id}/cancel", response_model=JobCancelado)
async def cancelar(job_id: Annotated[UUID, Path()], request: Request,
                   admin: Annotated[Principal, Depends(require_superuser)]):
    """Se ainda na fila, cancela na hora; se já rodando, marca a flag e o
    agente para no próximo heartbeat (mesma semântica do cancelar do treino)."""
    j = await db.fetch_one("SELECT id, type, status FROM job WHERE id = %s",
                           (job_id,))
    if j is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job não encontrado")
    if j["status"] in ("succeeded", "failed", "canceled"):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"job já terminou ({j['status']})")
    async with db.connection() as conn:
        async with conn.transaction():
            novo = (await db.fetch_row(
                "UPDATE job SET cancel_requested = true,"
                "  status = CASE WHEN status = 'queued' THEN 'canceled'"
                "                ELSE status END,"
                "  finished_at = CASE WHEN status = 'queued' THEN now()"
                "                     ELSE finished_at END"
                " WHERE id = %s RETURNING status", (job_id,), conn=conn))["status"]
            # treino cancelado na fila: o run espelha o job (como no cancel
            # da aba Treinamento) — senão fica "queued" para sempre na tela
            if novo == "canceled" and j["type"] == "train":
                await conn.execute(
                    "UPDATE training_run SET status = 'stopped',"
                    " finished_at = now() WHERE job_id = %s", (job_id,))
            await audit(admin, "job.cancel", "job", str(job_id),
                        ip=auth.client_ip(request), conn=conn)
    return {"id": job_id, "status": novo}
