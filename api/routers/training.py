"""Aba Treinamento: dispara um treino sobre uma versão congelada e acompanha.

A regra (base aceita, máquina-alvo válida, o que vai no payload do job) mora em
`services/training.py`. Aqui ficam a rota, o corpo validado e o RBAC.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi import Path as P
from pydantic import BaseModel, Field

from .. import auth
from ..auth import Principal
from ..deps import require_role
from ..services import training as svc

router = APIRouter(tags=["training"])


class RunIn(BaseModel):
    dataset_slug: str = Field(min_length=1)
    version: int = Field(ge=1)
    experiment: str = Field(min_length=1, max_length=120)
    model_slug: str = Field(default="detector", min_length=2, max_length=64)
    base_model: str = "yolo11n.pt"
    epochs: int = Field(default=100, ge=1, le=1000)
    imgsz: int = Field(default=640, ge=64, le=2048)
    batch: int = Field(default=16, ge=1, le=256)
    # máquina desejada: vazio = próxima livre do pool; um slug = fixa nela
    # (o job espera essa máquina liberar, mesmo se estiver ocupada).
    device_slug: str | None = Field(default=None, max_length=64)


@router.post("/projects/{project_slug}/runs",
             status_code=status.HTTP_201_CREATED)
async def criar_run(body: RunIn, project_slug: Annotated[str, P()],
                    request: Request,
                    actor: Annotated[Principal, Depends(require_role("engineer"))]):
    return await svc.criar_run(
        project_slug, actor, dataset_slug=body.dataset_slug,
        version=body.version, experiment=body.experiment,
        model_slug=body.model_slug, base_model=body.base_model,
        params={"epochs": body.epochs, "imgsz": body.imgsz, "batch": body.batch},
        device_slug=body.device_slug, ip=auth.client_ip(request))


@router.get("/train-devices",
            dependencies=[Depends(require_role("engineer"))])
async def train_devices():
    """Máquinas que podem ser alvo de um treino: capazes de treinar e ativas."""
    return await svc.maquinas_de_treino()


@router.get("/projects/{project_slug}/runs",
            dependencies=[Depends(require_role("viewer"))])
async def listar_runs(project_slug: Annotated[str, P()],
                      limite: int = Query(50, ge=1, le=200)):
    return await svc.listar_runs(project_slug, limite)


@router.get("/projects/{project_slug}/runs/{run_id}",
            dependencies=[Depends(require_role("viewer"))])
async def detalhe_run(project_slug: Annotated[str, P()], run_id: str):
    return await svc.detalhe_run(project_slug, run_id)


@router.get("/projects/{project_slug}/runs/{run_id}/stream",
            dependencies=[Depends(require_role("viewer"))])
async def stream_run(project_slug: Annotated[str, P()], run_id: str,
                     desde_seq: int = Query(0, ge=0),
                     desde_epoca: int = Query(-1, ge=-1)):
    """Leitura incremental para a tela ao vivo: eventos e métricas após o cursor."""
    return await svc.stream_run(project_slug, run_id, desde_seq=desde_seq,
                                desde_epoca=desde_epoca)


@router.post("/projects/{project_slug}/runs/{run_id}/cancel")
async def cancelar_run(project_slug: Annotated[str, P()], run_id: str,
                       request: Request,
                       actor: Annotated[Principal, Depends(require_role("engineer"))]):
    """Pede cancelamento. Na fila cancela na hora; rodando, o agente para no
    próximo heartbeat."""
    return await svc.cancelar_run(project_slug, run_id, actor,
                                  ip=auth.client_ip(request))
