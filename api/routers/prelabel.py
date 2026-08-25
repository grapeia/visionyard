"""Pré-tagueamento em lote: rodar um modelo sobre as imagens e devolver caixas
que o humano corrige.

Só HTTP: o corpo, o `response_model`, o RBAC e o código de status. A regra —
resolver a versão do modelo, conferir o escopo e enfileirar — mora em
`services/prelabel.py`.
"""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request, status
from pydantic import BaseModel, Field

from .. import auth
from ..auth import Principal
from ..deps import require_role
from ..services import prelabel as svc

router = APIRouter(tags=["prelabel"])


class PrelabelIn(BaseModel):
    model_slug: str = Field(min_length=1)
    version: int | None = None          # versão específica…
    alias: str | None = None            # …ou o apelido (champion por padrão)
    # escopo (exatamente um): dataset inteiro, uma imagem só (tecla w) ou um
    # lote específico de imagens (ex.: só a página aberta na rotulagem).
    # `UUID` e não `str`: id malformado é erro de entrada, e sem o tipo ele
    # chegava cru ao `::uuid[]` e virava 500 no cast do Postgres.
    dataset_slug: str | None = None
    sample_id: UUID | None = None
    sample_ids: list[UUID] | None = None
    conf: float = Field(default=0.25, ge=0.01, le=0.99)
    only_missing: bool = True           # pula quem já tem anotação minha


class PrelabelEnfileirado(BaseModel):
    job_id: UUID
    model_version: int
    status: str


class PrelabelsDescartados(BaseModel):
    removidos: int


@router.post("/projects/{project_slug}/prelabel",
             response_model=PrelabelEnfileirado,
             status_code=status.HTTP_201_CREATED)
async def enfileirar_prelabel(
        body: PrelabelIn, project_slug: Annotated[str, Path()],
        request: Request,
        actor: Annotated[Principal, Depends(require_role("engineer"))]):
    return await svc.enfileirar(
        project_slug, actor, model_slug=body.model_slug, version=body.version,
        alias=body.alias, dataset_slug=body.dataset_slug,
        sample_id=body.sample_id, sample_ids=body.sample_ids, conf=body.conf,
        only_missing=body.only_missing, ip=auth.client_ip(request))


@router.post("/projects/{project_slug}/datasets/{dataset_slug}/prelabel/discard",
             response_model=PrelabelsDescartados)
async def descartar_prelabels(
        project_slug: Annotated[str, Path()],
        dataset_slug: Annotated[str, Path()], request: Request,
        actor: Annotated[Principal, Depends(require_role("engineer"))]):
    """Remove os pré-tags rascunho das imagens deste dataset — de todos os
    autores. Só apaga saída de máquina não tocada (`origin='prelabel'` e
    `status='draft'`): anotação humana e pré-tag já corrigido (`correction`) ou
    revisado ficam intactos. Reverte as imagens para 'sem anotação'. Sem volta.
    """
    return await svc.descartar(project_slug, dataset_slug, actor,
                               auth.client_ip(request))
