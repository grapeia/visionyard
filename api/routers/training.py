"""Aba Treinamento: dispara um treino sobre uma versão congelada e acompanha.

A regra (base aceita, máquina-alvo válida, o que vai no payload do job) mora em
`services/training.py`. Aqui ficam a rota, o corpo validado e o RBAC.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi import Path as P
from pydantic import BaseModel, Field

from .. import auth
from ..auth import Principal
from ..deps import CurrentUser, require_action, require_role
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


class RunCriado(BaseModel):
    run_id: UUID
    job_id: UUID
    experimento: str
    status: str


class MaquinaDeTreino(BaseModel):
    """Só nome e estado: capabilities e token ficam com o superusuário, na aba
    Máquinas."""
    slug: str
    name: str
    status: str


class RunNaLista(BaseModel):
    id: UUID
    experimento: str
    base_model_name: str | None
    # O job e o run têm estados próprios: o job pode ter falhado sem que o run
    # tenha chegado a rodar, e a tela mostra os dois.
    job_status: str
    run_status: str
    metrics: dict[str, Any] | None
    best_epoch: int | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_s: int | None
    dataset: str
    dataset_version: int
    device: str | None
    criado_por: str | None


class VersaoProduzida(BaseModel):
    id: UUID
    model_slug: str
    version: int
    metrics: dict[str, Any] | None
    weights_uri: str | None
    bytes: int | None
    created_at: datetime


class DetalheDoRun(BaseModel):
    id: UUID
    job_id: UUID
    job_status: str
    job_error: str | None
    progress: dict[str, Any]
    run_status: str
    metrics: dict[str, Any] | None
    best_epoch: int | None
    params: dict[str, Any]
    base_model_name: str | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_s: int | None
    experimento: str
    dataset: str
    dataset_version: int
    device: str | None
    target_device: str | None
    # A versão de modelo que este treino produziu, se já registrada.
    model_version: VersaoProduzida | None


class EventoDoRun(BaseModel):
    seq: int
    at: datetime
    level: str
    message: str


class MetricaDoRun(BaseModel):
    epoch: int
    key: str
    value: float


class TrechoDoRun(BaseModel):
    """A leitura incremental da tela ao vivo: o cliente guarda os dois cursores
    (`seq` e `epoch`) e só pede o que falta."""
    job_status: str
    run_status: str
    progress: dict[str, Any]
    error: str | None
    metrics_finais: dict[str, Any] | None
    best_epoch: int | None
    eventos: list[EventoDoRun]
    metricas: list[MetricaDoRun]


class RunCancelado(BaseModel):
    run_id: UUID
    status: str


@router.post("/projects/{project_slug}/runs",
             response_model=RunCriado,
             status_code=status.HTTP_201_CREATED)
async def criar_run(body: RunIn, project_slug: Annotated[str, P()],
                    request: Request,
                    actor: Annotated[Principal,
                                     Depends(require_action("treino.runs.criar"))]):
    return await svc.criar_run(
        project_slug, actor, dataset_slug=body.dataset_slug,
        version=body.version, experiment=body.experiment,
        model_slug=body.model_slug, base_model=body.base_model,
        params={"epochs": body.epochs, "imgsz": body.imgsz, "batch": body.batch},
        device_slug=body.device_slug, ip=auth.client_ip(request))


@router.get("/train-devices", response_model=list[MaquinaDeTreino])
async def train_devices(user: CurrentUser):
    """Máquinas que podem ser alvo de um treino: capazes de treinar e ativas.

    Basta estar autenticado, como em `/deploy-devices` e pelo mesmo motivo: a
    rota não tem projeto no caminho, e papel é por projeto — `require_role`
    aqui pedia um `{project_slug}` que não existe e devolvia 422 para todo
    mundo, deixando o diálogo de novo treino sem máquina para fixar. O que
    protege o dado é o próprio SELECT, que devolve só nome e estado.
    """
    return await svc.maquinas_de_treino()


@router.get("/projects/{project_slug}/runs", response_model=list[RunNaLista],
            dependencies=[Depends(require_role("viewer"))])
async def listar_runs(project_slug: Annotated[str, P()],
                      limite: int = Query(50, ge=1, le=200)):
    return await svc.listar_runs(project_slug, limite)


@router.get("/projects/{project_slug}/runs/{run_id}",
            response_model=DetalheDoRun,
            dependencies=[Depends(require_role("viewer"))])
async def detalhe_run(project_slug: Annotated[str, P()],
                      run_id: Annotated[UUID, P()]):
    return await svc.detalhe_run(project_slug, run_id)


@router.get("/projects/{project_slug}/runs/{run_id}/stream",
            response_model=TrechoDoRun,
            dependencies=[Depends(require_role("viewer"))])
async def stream_run(project_slug: Annotated[str, P()],
                     run_id: Annotated[UUID, P()],
                     desde_seq: int = Query(0, ge=0),
                     desde_epoca: int = Query(-1, ge=-1)):
    """Leitura incremental para a tela ao vivo: eventos e métricas após o cursor."""
    return await svc.stream_run(project_slug, run_id, desde_seq=desde_seq,
                                desde_epoca=desde_epoca)


@router.post("/projects/{project_slug}/runs/{run_id}/cancel",
             response_model=RunCancelado)
async def cancelar_run(project_slug: Annotated[str, P()],
                       run_id: Annotated[UUID, P()],
                       request: Request,
                       actor: Annotated[Principal,
                                        Depends(require_action("treino.runs.cancelar"))]):
    """Pede cancelamento. Na fila cancela na hora; rodando, o agente para no
    próximo heartbeat."""
    return await svc.cancelar_run(project_slug, run_id, actor,
                                  ip=auth.client_ip(request))
