"""Importação de dados para um projeto.

`dry_run=true` responde na hora com o relatório do que aconteceria, sem gravar
nada — é o modo que se usa para conferir o mapeamento de classes antes de
mexer no projeto. Sem dry-run, a rota **enfileira** e responde: quem executa é
`api/worker.py`, dentro deste processo ou fora dele conforme
`import_worker_embedded`. A regra da fila mora em `services/imports.py`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field

from .. import auth, worker
from ..auth import Principal
from ..deps import require_action, require_role
from ..services import imports as svc

router = APIRouter(tags=["imports"])


class ImportIn(BaseModel):
    kind: Literal["label_studio_sqlite", "label_studio_json",
                  "labelme_json", "gcs_prefix"]
    # --- gcs_prefix ---
    bucket: str = ""
    prefix: str = ""
    limit: int | None = Field(default=None, ge=1)
    # --- label_studio_sqlite ---
    # Caminho do dump no servidor da API.
    sqlite_path: str = ""
    # Arquivo JSON, ou pasta de JSONs no caso LabelMe.
    json_path: str = ""
    # Onde os arquivos de imagem estão, para calcular sha256 e dimensões.
    image_root: str | None = None
    # Prefixo do URI final gravado no banco (ex: gs://bucket/frames).
    image_base_uri: str = ""
    ls_project_id: int | None = None
    # {"origem_a": "classe_a"} — resolve nome divergente entre origem e schema.
    class_overrides: dict[str, str] = Field(default_factory=dict)


class RelatorioDeImport(BaseModel):
    """O que a importação fez (ou faria). As listas são truncadas para não
    estourar o jsonb; `*_total` guarda o número real."""
    samples_created: int
    samples_existing: int
    annotations_created: int
    annotations_updated: int
    skipped_total: int
    skipped: list[dict[str, Any]]
    unmapped_classes: dict[str, int]
    warnings: list[str]
    skipped_truncado: str | None = None


class Simulacao(BaseModel):
    """`dry_run=true`: o relatório do que aconteceria, sem gravar nada."""
    dry_run: Literal[True]
    source: str
    source_classes: list[str]
    schema_classes: list[str]
    report: RelatorioDeImport


class ImportacaoEnfileirada(BaseModel):
    """`dry_run=false`. `source_id` vem junto porque quem chamou precisa
    vincular a origem a um dataset — adivinhar "a mais recente" seria corrida."""
    dry_run: Literal[False]
    job_id: UUID
    status: str
    source_id: UUID
    source: str


class JobDoProjeto(BaseModel):
    id: UUID
    type: str
    status: str
    progress: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None
    created_by: str | None


class EventoDeJob(BaseModel):
    seq: int
    at: datetime
    level: str
    message: str


class DetalheDeJob(BaseModel):
    id: UUID
    type: str
    status: str
    progress: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    events: list[EventoDeJob]


@router.post("/projects/{project_slug}/imports",
             response_model=Simulacao | ImportacaoEnfileirada)
async def create_import(body: ImportIn, project_slug: Annotated[str, Path()],
                        request: Request,
                        actor: Annotated[Principal,
                                         Depends(require_action("datasets.origens.importar"))],
                        dry_run: bool = True):
    """Importa (ou simula) dados de uma origem para o projeto.

    Default é `dry_run=true` de propósito: importar é uma operação que mexe no
    dado de todo mundo, e o custo de conferir antes é uma requisição.
    """
    if dry_run:
        return await svc.simular(project_slug, actor, body.model_dump())
    pedido = await svc.pedir(project_slug, actor, body.model_dump(),
                             auth.client_ip(request))
    # Acordar o laço embutido é assunto DESTE processo, não da regra: o worker
    # de fora nem é cutucado. Sem isso, começar uma importação passaria a
    # custar até `import_worker_poll_seconds`, e antes era imediato.
    worker.cutucar()
    return pedido


@router.get("/projects/{project_slug}/jobs",
            response_model=list[JobDoProjeto],
            dependencies=[Depends(require_role("viewer"))])
async def list_jobs(project_slug: Annotated[str, Path()],
                    type: str | None = None, limit: int = 20):
    """Jobs do projeto, do mais recente ao mais antigo."""
    return await svc.listar_jobs(project_slug, type, limit)


@router.get("/projects/{project_slug}/jobs/{job_id}",
            response_model=DetalheDeJob,
            dependencies=[Depends(require_role("viewer"))])
async def get_project_job(project_slug: Annotated[str, Path()],
                          job_id: Annotated[UUID, Path()]):
    return await svc.detalhe_do_job(project_slug, job_id)
