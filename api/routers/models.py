"""Aba Modelos: o registro. Versões imutáveis de um modelo e os apelidos que
apontam para elas.

A regra (o que é um apelido válido, qual artefato medir, se um build pode ir
para aquela máquina) mora em `services/models.py`. Aqui ficam a rota, o corpo
validado e o RBAC.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request, status
from pydantic import BaseModel, Field, model_validator

from .. import auth
from ..auth import Principal
from ..deps import require_role
from ..services import models as svc

router = APIRouter(tags=["models"])


class AliasIn(BaseModel):
    version: int = Field(ge=1)


class ModeloNaLista(BaseModel):
    slug: str
    name: str
    task_type: str
    created_at: datetime
    versoes: int
    ultima: int | None
    # {"champion": 3, "challenger": 4} — o apelido e a versão que ele aponta.
    aliases: dict[str, int] | None


class Build(BaseModel):
    """O artefato implantável de uma versão: onnx ou engine compilado.

    `target` é a variante compilada (`{"imgsz": 640, "precision": "fp16",
    "dla": true}`), e é ela que distingue dois builds do mesmo formato.
    """
    version: int
    format: str
    target: dict[str, Any]
    uri: str
    bytes: int | None
    bench: dict[str, Any] | None
    created_at: datetime
    built_on: str | None


class Avaliacao(BaseModel):
    """A mais recente por (versão, benchmark, artefato) — é o que permite
    comparar pt × onnx × engine × DLA lado a lado."""
    version: int
    benchmark: str
    benchmark_version: int
    format: str
    target: dict[str, Any] | None
    metrics: dict[str, Any] | None
    created_at: datetime


class VersaoDoModelo(BaseModel):
    id: UUID
    version: int
    metrics: dict[str, Any] | None
    weights_uri: str | None
    bytes: int | None
    status: str
    created_at: datetime
    run_id: UUID | None
    experimento: str | None
    dataset: str | None
    dataset_version: int | None
    builds: list[Build]
    avaliacoes: list[Avaliacao]


class ApelidoDoModelo(BaseModel):
    alias: str
    version: int
    moved_at: datetime
    por: str | None


class DetalheDoModelo(BaseModel):
    """`project_id` fica de fora: a linha do banco o traz para o serviço
    auditar, e antes deste modelo ele viajava para o navegador junto."""
    id: UUID
    slug: str
    name: str
    task_type: str
    created_at: datetime
    versoes: list[VersaoDoModelo]
    aliases: list[ApelidoDoModelo]


class JobEnfileirado(BaseModel):
    job_id: UUID
    version: int
    status: str


class AvaliacaoEnfileirada(JobEnfileirado):
    # Qual artefato será medido: 'pt', 'onnx', ou o engine com sua variante.
    artefato: str


class ImplantacaoPedida(BaseModel):
    deployment_id: UUID
    device: str
    state: str


class Implantacao(BaseModel):
    id: UUID
    device: str
    state: str
    requested_at: datetime
    activated_at: datetime | None
    error: str | None
    model_slug: str
    version: int
    format: str
    target: dict[str, Any]


class ApelidoDefinido(BaseModel):
    alias: str
    version: int


@router.get("/projects/{project_slug}/models",
            response_model=list[ModeloNaLista],
            dependencies=[Depends(require_role("viewer"))])
async def list_models(project_slug: Annotated[str, Path()]):
    return await svc.listar(project_slug)


@router.get("/projects/{project_slug}/models/{model_slug}",
            response_model=DetalheDoModelo,
            dependencies=[Depends(require_role("viewer"))])
async def get_model(project_slug: Annotated[str, Path()],
                    model_slug: Annotated[str, Path()]):
    return await svc.detalhe(project_slug, model_slug)


class ExportIn(BaseModel):
    format: Literal["onnx", "engine"] = "engine"
    imgsz: int = Field(default=640, ge=64, le=2048)
    # engine só: int8 é o alvo da linha (mais rápido no DLA), fp16 o seguro.
    precision: Literal["fp32", "fp16", "int8"] = "fp16"
    dla: bool = False
    # shapes dinâmicos: o engine aceita o mesmo letterbox retangular do .pt e
    # as detecções saem idênticas (medido em produção: 100% vs 96,7% com
    # shape fixo).
    dynamic: bool = True

    @model_validator(mode="after")
    def _dla_exige_shape_estatico(self):
        if self.dla:
            self.dynamic = False
        return self


@router.post("/projects/{project_slug}/models/{model_slug}/versions/{version}/export",
             response_model=JobEnfileirado,
             status_code=status.HTTP_201_CREATED)
async def exportar(body: ExportIn, project_slug: Annotated[str, Path()],
                   model_slug: Annotated[str, Path()], version: int,
                   request: Request,
                   actor: Annotated[Principal, Depends(require_role("engineer"))]):
    """Enfileira a compilação de uma versão para um formato implantável."""
    return await svc.enfileirar_export(
        project_slug, model_slug, version, actor, formato=body.format,
        imgsz=body.imgsz, precision=body.precision, dla=body.dla,
        dynamic=body.dynamic, ip=auth.client_ip(request))


class EvaluateIn(BaseModel):
    dataset_slug: str
    dataset_version: int = Field(ge=1)
    imgsz: int = Field(default=640, ge=64, le=2048)
    # QUAL artefato medir: 'pt' (o peso) ou um build compilado. precision/dla
    # escolhem a variante do engine — é assim que se compara int8 vs fp16 vs DLA.
    format: Literal["pt", "onnx", "engine"] = "pt"
    precision: str | None = None
    dla: bool | None = None


@router.post("/projects/{project_slug}/models/{model_slug}/versions/{version}/evaluate",
             response_model=AvaliacaoEnfileirada,
             status_code=status.HTTP_201_CREATED)
async def avaliar(body: EvaluateIn, project_slug: Annotated[str, Path()],
                  model_slug: Annotated[str, Path()], version: int,
                  request: Request,
                  actor: Annotated[Principal, Depends(require_role("engineer"))]):
    """Enfileira a medição de um artefato (peso ou build) contra um benchmark."""
    return await svc.enfileirar_avaliacao(
        project_slug, model_slug, version, actor,
        dataset_slug=body.dataset_slug, dataset_version=body.dataset_version,
        imgsz=body.imgsz, formato=body.format, precision=body.precision,
        dla=body.dla, ip=auth.client_ip(request))


class DeployIn(BaseModel):
    version: int = Field(ge=1)
    format: Literal["onnx", "engine"] = "engine"
    device_slug: str


@router.post("/projects/{project_slug}/models/{model_slug}/deploy",
             response_model=ImplantacaoPedida,
             status_code=status.HTTP_201_CREATED)
async def implantar(body: DeployIn, project_slug: Annotated[str, Path()],
                    model_slug: Annotated[str, Path()], request: Request,
                    actor: Annotated[Principal, Depends(require_role("engineer"))]):
    """Marca um build para ser implantado num dispositivo."""
    return await svc.implantar(
        project_slug, model_slug, actor, version=body.version,
        formato=body.format, device_slug=body.device_slug,
        ip=auth.client_ip(request))


@router.get("/projects/{project_slug}/deployments",
            response_model=list[Implantacao],
            dependencies=[Depends(require_role("viewer"))])
async def list_deployments(project_slug: Annotated[str, Path()]):
    """Implantações do projeto, com device, build e estado."""
    return await svc.listar_deployments(project_slug)


@router.put("/projects/{project_slug}/models/{model_slug}/aliases/{alias}",
            response_model=ApelidoDefinido)
async def set_alias(project_slug: Annotated[str, Path()],
                    model_slug: Annotated[str, Path()],
                    alias: Annotated[str, Path()], body: AliasIn,
                    request: Request,
                    actor: Annotated[Principal, Depends(require_role("engineer"))]):
    """Aponta um apelido para uma versão (promover). A versão não muda."""
    return await svc.definir_alias(project_slug, model_slug, alias, body.version,
                                   actor, ip=auth.client_ip(request))


@router.delete("/projects/{project_slug}/models/{model_slug}/aliases/{alias}",
               status_code=status.HTTP_204_NO_CONTENT)
async def remove_alias(project_slug: Annotated[str, Path()],
                       model_slug: Annotated[str, Path()],
                       alias: Annotated[str, Path()], request: Request,
                       actor: Annotated[Principal, Depends(require_role("engineer"))]):
    await svc.remover_alias(project_slug, model_slug, alias, actor,
                            ip=auth.client_ip(request))
