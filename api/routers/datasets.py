"""Aba Dataset: definição, origens, prévia e congelamento de versões.

Só HTTP: o corpo, o `response_model`, o RBAC e o código de status. A regra —
resolver as origens, sortear o split, selar a versão sob lock — mora em
`services/datasets.py`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from fastapi import Path as P
from pydantic import BaseModel, Field, field_validator, model_validator

from .. import auth
from ..auth import Principal
from ..deps import require_role
from ..services import datasets as svc
from .projects import SLUG_RE

router = APIRouter(tags=["datasets"])


class Roi(BaseModel):
    """Região de interesse: caixa normalizada [x,y,w,h] em frações 0–1.

    `x,y,w,h` é a caixa canônica que o pipeline recorta (treino, avaliação e
    inferência aplicam a MESMA). `anchor`/`w_pct`/`h_pct` são só dica da UI para
    repopular os presets — o engine ignora e usa a caixa.
    """
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)
    anchor: str | None = Field(default=None, max_length=20)
    w_pct: float | None = Field(default=None, ge=0.0, le=1.0)
    h_pct: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _dentro(self) -> Roi:
        # Ultrapassar a borda cortaria fora da imagem: o recorte em pixels ficaria
        # sem sentido e o des-recorte na inferência escalaria errado.
        if self.x + self.w > 1.0 + 1e-6 or self.y + self.h > 1.0 + 1e-6:
            raise ValueError("ROI ultrapassa a borda da imagem (x+w ou y+h > 1)")
        return self


class DatasetIn(BaseModel):
    slug: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["train", "eval", "benchmark"] = "train"
    roi: Roi | None = None

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not SLUG_RE.match(v):
            raise ValueError("slug deve ser minúsculo, com hífens")
        return v


class RoiIn(BaseModel):
    """Corpo do PATCH de ROI. `roi: null` remove o recorte do dataset."""
    roi: Roi | None = None


class FreezeIn(BaseModel):
    descricao: str = Field(min_length=1, max_length=500)
    split_method: Literal["random", "by_group"] = "random"
    val_ratio: float = Field(default=0.2, ge=0.05, le=0.5)
    seed: int = 42
    group_by: str = "metadata.lote"
    only_annotated: bool = True


class DatasetNaLista(BaseModel):
    slug: str
    name: str
    kind: str
    roi: Roi | None
    created_at: datetime
    origens: int
    versoes: int
    ultima_versao: int | None


class DatasetCriado(BaseModel):
    id: UUID
    slug: str
    name: str
    kind: str
    roi: Roi | None
    created_at: datetime


class RoiDefinida(BaseModel):
    slug: str
    roi: Roi | None


class OrigemDoDataset(BaseModel):
    id: UUID
    kind: str
    name: str | None
    uri: str | None
    added_at: datetime
    imagens: int


class VersaoCongelada(BaseModel):
    version: int
    descricao: str | None
    stats: dict[str, Any]
    split_policy: dict[str, Any]
    roi: Roi | None
    manifest_uri: str
    frozen_at: datetime
    criado_por: str | None


class DetalheDoDataset(BaseModel):
    """A definição do dataset, as origens e as versões já seladas.

    `project_id` e `proj` ficam de fora de propósito: a linha do banco carrega
    os dois para o handler resolver o RBAC, e antes deste modelo os dois
    viajavam para o navegador só porque estavam no `SELECT`.
    """
    id: UUID
    slug: str
    name: str
    kind: str
    roi: Roi | None
    origens: list[OrigemDoDataset]
    versoes: list[VersaoCongelada]
    # Pré-tags rascunho por revisar: é o que o botão "Descartar" remove.
    pretags_rascunho: int


class PreviaDoDataset(BaseModel):
    """O que entraria numa versão congelada agora. `sem_md5` sai daqui para a
    tela não oferecer um botão que sempre daria 409."""
    imagens_nas_origens: int
    anotadas: int
    pendentes: int
    entrariam: int
    sem_dimensao: int
    sem_md5: int
    proxima_versao: int


class Congelamento(BaseModel):
    version: int
    manifest_uri: str
    items_sha256: str
    stats: dict[str, Any]
    split_policy: dict[str, Any]


class DiffDeVersoes(BaseModel):
    de: int | None
    para: int
    entraram: list[str]
    sairam: list[str]
    # Só na primeira versão, onde não há contra o que comparar.
    nota: str | None = None


@router.get("/projects/{project_slug}/datasets",
            response_model=list[DatasetNaLista],
            dependencies=[Depends(require_role("viewer"))])
async def list_datasets(project_slug: Annotated[str, P()]):
    return await svc.listar(project_slug)


@router.post("/projects/{project_slug}/datasets",
             response_model=DatasetCriado,
             status_code=status.HTTP_201_CREATED)
async def create_dataset(
        body: DatasetIn, project_slug: Annotated[str, P()], request: Request,
        actor: Annotated[Principal, Depends(require_role("engineer"))]):
    return await svc.criar(
        project_slug, actor, slug=body.slug, name=body.name, kind=body.kind,
        roi=body.roi.model_dump() if body.roi else None,
        ip=auth.client_ip(request))


@router.patch("/projects/{project_slug}/datasets/{dataset_slug}",
              response_model=RoiDefinida)
async def update_roi(body: RoiIn, project_slug: Annotated[str, P()],
                     dataset_slug: Annotated[str, P()], request: Request,
                     actor: Annotated[Principal, Depends(require_role("engineer"))]):
    """Define ou remove a ROI do dataset (`roi: null` remove).

    Só afeta as PRÓXIMAS versões congeladas — as já existentes carregam a ROI
    com que foram congeladas (imutável) e seguem reproduzíveis. Para um dataset
    já congelado passar a treinar com ROI, mude aqui e congele uma nova versão.
    """
    return await svc.definir_roi(
        project_slug, dataset_slug, actor,
        roi=body.roi.model_dump() if body.roi else None,
        ip=auth.client_ip(request))


@router.get("/projects/{project_slug}/datasets/{dataset_slug}",
            response_model=DetalheDoDataset,
            dependencies=[Depends(require_role("viewer"))])
async def get_dataset(project_slug: Annotated[str, P()],
                      dataset_slug: Annotated[str, P()]):
    return await svc.detalhe(project_slug, dataset_slug)


@router.put("/projects/{project_slug}/datasets/{dataset_slug}/sources/{source_id}",
            status_code=status.HTTP_204_NO_CONTENT)
async def add_origin(project_slug: Annotated[str, P()],
                     dataset_slug: Annotated[str, P()], source_id: str,
                     request: Request,
                     actor: Annotated[Principal, Depends(require_role("engineer"))]):
    await svc.vincular_origem(project_slug, dataset_slug, actor,
                              source_id=source_id, ip=auth.client_ip(request))


@router.delete("/projects/{project_slug}/datasets/{dataset_slug}/sources/{source_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def remove_origin(project_slug: Annotated[str, P()],
                        dataset_slug: Annotated[str, P()], source_id: str,
                        request: Request,
                        actor: Annotated[Principal, Depends(require_role("engineer"))]):
    """Remove a origem da definição.

    Não apaga sample nem anotação, e não toca nas versões já congeladas: elas
    continuam treináveis e reproduzíveis. Só as próximas versões deixarão de
    incluir essas imagens.
    """
    await svc.desvincular_origem(project_slug, dataset_slug, actor,
                                 source_id=source_id,
                                 ip=auth.client_ip(request))


@router.get("/projects/{project_slug}/datasets/{dataset_slug}/preview",
            response_model=PreviaDoDataset,
            dependencies=[Depends(require_role("viewer"))])
async def preview(project_slug: Annotated[str, P()],
                  dataset_slug: Annotated[str, P()],
                  only_annotated: bool = True):
    """O que entraria numa versão congelada agora — sem gravar nada."""
    return await svc.previa(project_slug, dataset_slug, only_annotated)


@router.post("/projects/{project_slug}/datasets/{dataset_slug}/freeze",
             response_model=Congelamento,
             status_code=status.HTTP_201_CREATED)
async def freeze(body: FreezeIn, project_slug: Annotated[str, P()],
                 dataset_slug: Annotated[str, P()], request: Request,
                 actor: Annotated[Principal, Depends(require_role("engineer"))]):
    return await svc.congelar(
        project_slug, dataset_slug, actor, descricao=body.descricao,
        split_method=body.split_method, val_ratio=body.val_ratio,
        seed=body.seed, group_by=body.group_by,
        only_annotated=body.only_annotated, ip=auth.client_ip(request))


@router.get("/projects/{project_slug}/datasets/{dataset_slug}/versions/{version}/diff",
            response_model=DiffDeVersoes,
            dependencies=[Depends(require_role("viewer"))])
async def diff(project_slug: Annotated[str, P()],
               dataset_slug: Annotated[str, P()], version: int,
               contra: int | None = None):
    """O que entrou e o que saiu entre duas versões.

    Responde pelo índice (`dataset_version_item`), sem baixar manifesto.
    """
    return await svc.diff(project_slug, dataset_slug, version, contra)
