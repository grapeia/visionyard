"""Fila global de jobs: a visão operacional da aba Máquinas.

Só HTTP: o `response_model`, o RBAC e o código de status. A regra — o que a
fila mostra e o que cancelar significa — mora em `services/jobs.py`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel

from .. import auth
from ..auth import Principal
from ..deps import require_superuser
from ..services import jobs as svc

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
    """
    return await svc.fila()


@router.post("/jobs/{job_id}/cancel", response_model=JobCancelado)
async def cancelar(job_id: Annotated[UUID, Path()], request: Request,
                   admin: Annotated[Principal, Depends(require_superuser)]):
    """Se ainda na fila, cancela na hora; se já rodando, marca a flag e o
    agente para no próximo heartbeat (mesma semântica do cancelar do treino)."""
    return await svc.cancelar(job_id, admin, auth.client_ip(request))
