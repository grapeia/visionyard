"""Aba Dados: imagens do projeto e a anotação de cada uma.

A regra (o que é uma caixa válida, de quem é a anotação, o que a grade conta)
mora em `services/samples.py`. Aqui ficam a rota, o corpo validado, o RBAC e o
que a rota devolve.

As respostas são declaradas (`response_model`) e não a linha do banco: sem
isso, coluna acrescentada a um `SELECT` viajava para o navegador sem ninguém
decidir, e o OpenAPI não descrevia saída nenhuma. É a aba que o `app.js`
consome com mais força — foi por onde o item 14 do plano começou.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field, field_validator

from ..auth import Principal
from ..deps import CurrentUser, projeto_ou_404, require_role
from ..services import samples as svc

router = APIRouter(tags=["samples"])


class Caixa(BaseModel):
    cls: int = Field(ge=0)
    cx: float
    cy: float
    w: float = Field(gt=0)
    h: float = Field(gt=0)

    @field_validator("cx", "cy")
    @classmethod
    def _dentro(cls, v: float) -> float:
        # Caixa com centro fora da imagem é sempre erro de conversão ou de
        # arrasto — deixar entrar contamina o manifesto, que é imutável.
        if not 0.0 <= v <= 1.0:
            raise ValueError("centro da caixa fora da imagem")
        return v


class AnotacaoIn(BaseModel):
    # Lista vazia é significativa: quer dizer "olhei e não há nada aqui" —
    # um exemplo negativo, diferente de "ainda não olhei".
    boxes: list[Caixa]
    status: Literal["submitted", "draft"] = "submitted"


class ItemDaGrade(BaseModel):
    """Um cartão da grade. `annotation_id` nulo é "ainda não olhei";
    `n_caixas` zero com id preenchido é "olhei e não há nada"."""
    id: UUID
    uri: str
    width: int | None
    height: int | None
    metadata: dict[str, Any] | None
    annotation_id: UUID | None
    status: str | None
    origin: str | None
    n_caixas: int
    url: str | None


class PaginaDeImagens(BaseModel):
    pagina: int
    por_pagina: int
    total: int
    # Falso quando a credencial não assina URL: a tela troca a miniatura por um
    # aviso em vez de mostrar quadro quebrado.
    assina: bool
    itens: list[ItemDaGrade]


class MinhaAnotacao(BaseModel):
    id: UUID
    # `list[Caixa]` de propósito não: a pré-rotulagem grava `conf` em cada
    # caixa, e um modelo estrito a apagaria na saída — a confiança do modelo
    # some do que o rotulador recebe sem ninguém notar.
    boxes: list[dict[str, Any]]
    status: str
    origin: str
    updated_at: datetime


class DetalheDaImagem(BaseModel):
    id: UUID
    uri: str
    width: int | None
    height: int | None
    metadata: dict[str, Any] | None
    url: str | None
    minha_anotacao: MinhaAnotacao | None
    # Só a contagem: ver a caixa alheia antes de desenhar a sua enviesa a
    # anotação e destrói a medida de concordância.
    outros_anotaram: int


class AnotacaoGravada(BaseModel):
    id: UUID
    status: str
    origin: str
    caixas: int


@router.get("/projects/{project_slug}/samples",
            response_model=PaginaDeImagens,
            dependencies=[Depends(require_role("viewer"))])
async def listar(project_slug: Annotated[str, Path()], user: CurrentUser,
                 pagina: int = Query(0, ge=0),
                 filtro: Literal["todas", "pendentes", "anotadas"] = "todas",
                 dataset: str | None = None):
    proj = await projeto_ou_404(project_slug)
    return await svc.listar(proj["id"], user.id, pagina=pagina, filtro=filtro,
                            dataset=dataset)


@router.get("/projects/{project_slug}/samples/{sample_id}",
            response_model=DetalheDaImagem,
            dependencies=[Depends(require_role("viewer"))])
async def detalhe(project_slug: Annotated[str, Path()],
                  sample_id: Annotated[UUID, Path()], user: CurrentUser):
    return await svc.detalhe(project_slug, sample_id, user.id)


@router.put("/projects/{project_slug}/samples/{sample_id}/annotation",
            response_model=AnotacaoGravada)
async def salvar(body: AnotacaoIn, project_slug: Annotated[str, Path()],
                 sample_id: Annotated[UUID, Path()],
                 user: Annotated[Principal, Depends(require_role("annotator"))]):
    """Grava a SUA anotação. A de outro rotulador não é tocada."""
    return await svc.salvar_anotacao(
        project_slug, sample_id, user.id,
        [b.model_dump() for b in body.boxes], body.status)
