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

from fastapi import APIRouter, Depends, Path, Query, Response
from pydantic import BaseModel, Discriminator, Field, Tag, field_validator

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


class Poligono(BaseModel):
    """Contorno de projeto de segmentação: vértices normalizados, em ordem."""
    cls: int = Field(ge=0)
    pts: list[tuple[float, float]] = Field(min_length=3)

    @field_validator("pts")
    @classmethod
    def _valido(cls, v: list[tuple[float, float]]) -> list[tuple[float, float]]:
        # Mesmo motivo da caixa. Recortar aqui em silêncio esconderia o erro
        # de conversão que pôs o vértice lá fora.
        if any(not (0.0 <= c <= 1.0) for p in v for c in p):
            raise ValueError("vértice do polígono fora da imagem")
        # Três pontos repetidos ou em linha passam na contagem e não têm
        # área: o YOLO-seg recebe uma máscara vazia, e com ROI o objeto some
        # ou aparece conforme o recorte. Auto-interseção fica com o editor.
        area = sum(v[i - 1][0] * p[1] - p[0] * v[i - 1][1]
                   for i, p in enumerate(v)) / 2
        if len(set(v)) < 3 or abs(area) < 1e-9:
            raise ValueError("polígono sem área")
        return v


def _forma(v: Any) -> str:
    # Pela chave, e não "a primeira que validar": a caixa ignora chave extra
    # (a pré-rotulagem grava `conf` nela), então um polígono com `cx` perdido
    # validaria como caixa e perderia os vértices. A instância conta também:
    # é o que o pydantic entrega ao serializar (`model_dump`).
    if isinstance(v, dict):
        return "poligono" if "pts" in v else "caixa"
    return "poligono" if isinstance(v, Poligono) else "caixa"


ItemDaAnotacao = Annotated[
    Annotated[Caixa, Tag("caixa")] | Annotated[Poligono, Tag("poligono")],
    Discriminator(_forma)]


class AnotacaoIn(BaseModel):
    # Lista vazia é significativa: quer dizer "olhei e não há nada aqui" —
    # um exemplo negativo, diferente de "ainda não olhei". Caixa ou polígono
    # conforme o tipo do projeto — quem confere é o serviço, que o conhece.
    boxes: list[ItemDaAnotacao]
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
    # De onde o SAM do anotador lê os pixels: a rota `sam-image` da API, ou a
    # própria `url` assinada (parâmetro `sam_image_source`). Nulo fora de
    # segmentação.
    url_sam: str | None
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


@router.get("/projects/{project_slug}/samples/{sample_id}/sam-image",
            response_class=Response,
            responses={200: {"content": {"image/jpeg": {}},
                             "description": "JPEG reduzido a 1024 px"}},
            dependencies=[Depends(require_role("viewer"))])
async def imagem_sam(project_slug: Annotated[str, Path()],
                     sample_id: Annotated[UUID, Path()]):
    """A imagem para o contorno automático, do mesmo endereço da tela."""
    corpo = await svc.imagem_sam(project_slug, sample_id)
    # `private`: a imagem é do projeto, não vai para cache compartilhado
    return Response(corpo, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=300"})


@router.put("/projects/{project_slug}/samples/{sample_id}/annotation",
            response_model=AnotacaoGravada)
async def salvar(body: AnotacaoIn, project_slug: Annotated[str, Path()],
                 sample_id: Annotated[UUID, Path()],
                 user: Annotated[Principal, Depends(require_role("annotator"))]):
    """Grava a SUA anotação. A de outro rotulador não é tocada."""
    return await svc.salvar_anotacao(
        project_slug, sample_id, user.id,
        [b.model_dump() for b in body.boxes], body.status)
