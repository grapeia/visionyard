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

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from .. import auth, db, worker
from ..auth import Principal
from ..deps import projeto_ou_404, require_role
from ..importers.base import ImportReport
from ..importers.runner import run_import
from ..services import imports as svc

router = APIRouter(tags=["imports"])


class ImportIn(BaseModel):
    kind: Literal["label_studio_sqlite", "gcs_prefix"]
    # --- gcs_prefix ---
    bucket: str = ""
    prefix: str = ""
    limit: int | None = Field(default=None, ge=1)
    # --- label_studio_sqlite ---
    # Caminho do dump no servidor da API.
    sqlite_path: str = ""
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
                        actor: Annotated[Principal, Depends(require_role("engineer"))],
                        dry_run: bool = True):
    """Importa (ou simula) dados de uma origem para o projeto.

    Default é `dry_run=true` de propósito: importar é uma operação que mexe no
    dado de todo mundo, e o custo de conferir antes é uma requisição.

    Sem dry-run a rota **não executa**: ela enfileira e responde. O importador
    ainda é construído aqui porque é o que valida a entrada — caminho fora de
    `import_root`, dump inexistente, bucket em branco — e o que descreve a
    origem na resposta. Quem o constrói de novo para executar é o worker, a
    partir da mesma linha de `job.payload`.
    """
    proj = await projeto_ou_404(project_slug)
    ctx = await svc.contexto_do_projeto(str(proj["id"]))
    importador = svc.construir_importador(body.model_dump())

    if dry_run:
        report = ImportReport()
        await run_import(project_id=ctx["project_id"],
                         schema_id=ctx["schema_id"],
                         schema_classes=ctx["classes"], importer=importador,
                         actor_id=actor.id,
                         class_overrides=body.class_overrides,
                         source_id=None, dry_run=True, report=report)
        return {"dry_run": True, "source": importador.describe(),
                "source_classes": importador.classes(),
                "schema_classes": ctx["classes"], "report": report.as_dict()}

    uri = (f"gs://{body.bucket.strip('/')}/{body.prefix.lstrip('/')}"
           if body.kind == "gcs_prefix" else body.sqlite_path)
    source_id = await svc.upsert_source(
        ctx["project_id"],
        "gcs" if body.kind == "gcs_prefix" else "import",
        uri, importador.describe(), body.model_dump(), actor.id)

    job_id = await svc.enfileirar(
        actor, project_id=ctx["project_id"], kind=body.kind,
        payload=body.model_dump(), source_id=source_id,
        ip=auth.client_ip(request))
    # O laço embutido dorme entre polls; sem o cutucão, começar uma importação
    # passaria a custar até `import_worker_poll_seconds`, e antes era imediato.
    worker.cutucar()

    # Devolve a origem criada/reaproveitada: quem chamou precisa vinculá-la a
    # um dataset, e adivinhar "a mais recente" depois seria uma corrida.
    return {"dry_run": False, "job_id": job_id, "status": "queued",
            "source_id": source_id, "source": importador.describe()}


@router.get("/projects/{project_slug}/jobs",
            response_model=list[JobDoProjeto],
            dependencies=[Depends(require_role("viewer"))])
async def list_jobs(project_slug: Annotated[str, Path()],
                    type: str | None = None, limit: int = 20):
    """Jobs do projeto, do mais recente ao mais antigo.

    Recolhe antes de listar: importação que parou de bater ponto ficaria em
    `running` para sempre, e esta tela é onde ela é vista. É escrita num GET
    de propósito — não há nada periódico no processo, e numa instalação sem
    agente nem o reaper do claim roda.
    """
    await svc.recolher_orfaos()
    return await db.fetch_all(
        "SELECT j.id, j.type, j.status, j.progress, j.result, j.error,"
        "       j.created_at, j.finished_at, u.email AS created_by"
        "  FROM job j JOIN project p ON p.id = j.project_id"
        "  LEFT JOIN app_user u ON u.id = j.created_by"
        " WHERE p.slug = %s AND (%s::text IS NULL OR j.type = %s)"
        " ORDER BY j.created_at DESC LIMIT %s",
        (project_slug, type, type, min(limit, 100)))


@router.get("/projects/{project_slug}/jobs/{job_id}",
            response_model=DetalheDeJob,
            dependencies=[Depends(require_role("viewer"))])
async def get_project_job(project_slug: Annotated[str, Path()],
                          job_id: Annotated[UUID, Path()]):
    job = await db.fetch_one(
        "SELECT j.id, j.type, j.status, j.progress, j.result, j.error,"
        "       j.created_at, j.started_at, j.finished_at"
        "  FROM job j JOIN project p ON p.id = j.project_id"
        " WHERE p.slug = %s AND j.id = %s", (project_slug, job_id))
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job não encontrado")
    events = await db.fetch_all(
        "SELECT seq, at, level, message FROM job_event"
        " WHERE job_id = %s ORDER BY seq", (job_id,))
    return {**job, "events": events}
