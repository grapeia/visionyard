"""Cadastro de máquinas que executam jobs (agentes de treino e inferência).

Só HTTP: o corpo, o `response_model`, o RBAC e o código de status. A regra —
e o porquê de device ser da instalação e não de um projeto — mora em
`services/devices.py`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request, status
from pydantic import BaseModel, Field, field_validator

from .. import auth
from ..auth import Principal
from ..deps import CurrentUser, require_superuser
from ..services import devices as svc
from .projects import SLUG_RE

router = APIRouter(tags=["devices"])


class DeviceIn(BaseModel):
    slug: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    device_class: str = Field(min_length=1, max_length=64)  # 'gpu-server'
    # {"train": true, "gpu": "...", "vram_gb": 16} ou
    # {"infer": true, "gpu": "...", "cuda": "12.2"}
    capabilities: dict = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not SLUG_RE.match(v):
            raise ValueError("slug deve ser minúsculo, com hífens")
        return v


class Maquina(BaseModel):
    """`capabilities` é o que decide o roteamento: o claim só entrega o job a
    quem contém o que ele exige."""
    id: UUID
    slug: str
    name: str
    device_class: str
    capabilities: dict[str, Any]
    labels: list[str]
    status: str
    last_heartbeat: datetime | None
    registered_at: datetime
    rodando: int


class MaquinaDeImplantacao(BaseModel):
    """Sem capabilities nem token: esta lista é visível a qualquer usuário
    autenticado, porque o engenheiro escolhe o alvo ao implantar."""
    slug: str
    name: str
    device_class: str
    status: str


class MaquinaCadastrada(BaseModel):
    id: UUID
    slug: str
    name: str
    device_class: str
    capabilities: dict[str, Any]
    labels: list[str]
    status: str
    # O token em claro existe só aqui — quem cadastra copia para o .env do
    # agente e não o vê de novo.
    token: str
    aviso: str


class TokenRotacionado(BaseModel):
    slug: str
    token: str
    aviso: str


class EstadoDaMaquina(BaseModel):
    id: UUID
    slug: str
    status: str


@router.get("/devices", response_model=list[Maquina],
            dependencies=[Depends(require_superuser)])
async def list_devices():
    return await svc.listar()


@router.get("/deploy-devices", response_model=list[MaquinaDeImplantacao])
async def deploy_devices(user: CurrentUser):
    """Máquinas que podem receber uma implantação. Visível a qualquer usuário
    autenticado (o engenheiro precisa escolher o alvo ao implantar), mas só
    nome/classe/estado — capabilities e token ficam com o superusuário."""
    return await svc.listar_para_implantacao()


@router.post("/devices", response_model=MaquinaCadastrada,
             status_code=status.HTTP_201_CREATED)
async def create_device(body: DeviceIn, request: Request,
                        admin: Annotated[Principal, Depends(require_superuser)]):
    dev = await svc.cadastrar(admin, slug=body.slug, name=body.name,
                              device_class=body.device_class,
                              capabilities=body.capabilities,
                              labels=body.labels, ip=auth.client_ip(request))
    # O token em claro existe só aqui — quem cadastra copia para o .env do
    # agente e não o vê de novo.
    return {**dev, "aviso": "guarde o token: ele não é mostrado de novo"}


@router.post("/devices/{slug}/token", response_model=TokenRotacionado)
async def rotate_token(slug: Annotated[str, Path()], request: Request,
                       admin: Annotated[Principal, Depends(require_superuser)]):
    novo = await svc.rotacionar_token(slug, admin, auth.client_ip(request))
    return {**novo, "aviso": "o token anterior deixou de valer agora"}


@router.delete("/devices/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(slug: Annotated[str, Path()], request: Request,
                        admin: Annotated[Principal, Depends(require_superuser)]):
    """Exclui uma máquina. Recusa se ela tem histórico de jobs — `job.device_id`
    guarda quem EXECUTOU cada treino, e apagar a máquina apagaria esse rastro.
    Para retirar uma máquina em uso, o caminho é `disabled`."""
    await svc.excluir(slug, admin, auth.client_ip(request))


@router.post("/devices/{slug}/status", response_model=EstadoDaMaquina)
async def set_status(slug: Annotated[str, Path()],
                     estado: Literal["idle", "draining", "disabled"],
                     request: Request,
                     admin: Annotated[Principal, Depends(require_superuser)]):
    """`draining` tira do pool sem apagar; `disabled` também invalida o token.
    `busy`/`offline` são do agente (heartbeat), não se define à mão."""
    return await svc.definir_estado(slug, estado, admin,
                                    auth.client_ip(request))
