"""Navegação de bucket: pastas, objetos e miniaturas.

Só HTTP: o `response_model` e o RBAC. A regra — a cerca da allowlist, o
`delimiter` que faz a listagem caber, e as miniaturas assinadas — mora em
`services/buckets.py`.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi import Path as P
from pydantic import BaseModel

from ..deps import require_role
from ..services import buckets as svc

router = APIRouter(tags=["buckets"])


class BucketConfigurado(BaseModel):
    nome: str


class ObjetoDoBucket(BaseModel):
    nome: str
    caminho: str
    bytes: int | None
    imagem: bool
    # Só as primeiras imagens ganham URL assinada, e só se a credencial assina.
    url: str | None


class Navegacao(BaseModel):
    bucket: str
    prefixo: str
    # Nulo na raiz: não há um nível acima.
    pai: str | None
    pastas: list[str]
    objetos: list[ObjetoDoBucket]
    tem_mais: bool
    imagens: int
    assina: bool


@router.get("/projects/{project_slug}/buckets",
            response_model=list[BucketConfigurado],
            dependencies=[Depends(require_role("engineer"))])
async def listar_buckets(project_slug: Annotated[str, P()]):
    """Buckets pré-definidos na instalação.

    Escopado a projeto e exigindo `engineer`: navegar o bucket é passo de
    compor dataset, não leitura pública. Sem isso, qualquer usuário
    autenticado — inclusive um `annotator` terceirizado — enxergaria o
    conteúdo de todos os buckets da instalação.
    """
    return await svc.configurados()


@router.get("/projects/{project_slug}/buckets/browse",
            response_model=Navegacao,
            dependencies=[Depends(require_role("engineer"))])
async def navegar(project_slug: Annotated[str, P()],
                  bucket: Annotated[str, Query(min_length=1)],
                  prefixo: str = ""):
    return await svc.navegar(bucket, prefixo)
