"""Importação de dados para um projeto.

`dry_run=true` responde na hora com o relatório do que aconteceria, sem gravar
nada — é o modo que se usa para conferir o mapeamento de classes antes de
mexer no projeto. Sem dry-run, cria um `job` e executa em segundo plano.

A execução é in-process porque importar não usa GPU: o que ela consome é
I/O de disco/rede. Quando houver origem grande o bastante para justificar,
o mesmo job pode ser reivindicado por um agente sem mudar o contrato — é por
isso que ela já nasce como `job` e não como uma chamada síncrona qualquer.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path as FsPath
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from .. import auth, db
from ..audit import audit
from ..auth import Principal
from ..config import settings
from ..deps import require_role
from ..importers.base import ImportReport
from ..importers.gcs_prefix import GCSPrefixImporter
from ..importers.label_studio import LabelStudioImporter
from ..importers.runner import run_import
from ..services import imports as svc

router = APIRouter(tags=["imports"])

logger = logging.getLogger(__name__)

# Referências fortes das importações em curso (ver create_import).
_EM_ANDAMENTO: set = set()


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


def _dentro_da_raiz(caminho: str) -> FsPath:
    """Resolve o caminho e exige que ele esteja sob `settings.import_root`.

    `resolve()` antes da comparação porque `../` e symlink escapariam de uma
    checagem textual.
    """
    raiz = FsPath(settings.import_root).resolve()
    alvo = FsPath(caminho).resolve()
    if raiz != alvo and raiz not in alvo.parents:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"caminho fora da pasta de importação ({settings.import_root})")
    return alvo


def _build_importer(body: ImportIn):
    if body.kind == "gcs_prefix":
        if not body.bucket.strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "informe o bucket")
        return GCSPrefixImporter(bucket=body.bucket, prefix=body.prefix,
                                 limit=body.limit)
    if not body.sqlite_path:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "informe o caminho do dump do Label Studio")
    if not body.image_root:
        # Sem isto o importador pula TODOS os samples (não tem como calcular a
        # identidade) e o job termina com "succeeded" e zero importados.
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "informe onde estão as imagens (image_root)")
    caminho = _dentro_da_raiz(body.sqlite_path)
    raiz_img = _dentro_da_raiz(body.image_root)
    try:
        return LabelStudioImporter(
            sqlite_path=str(caminho), image_root=str(raiz_img),
            image_base_uri=body.image_base_uri, project_id=body.ls_project_id)
    except FileNotFoundError:
        # Mensagem sem o caminho: dizer "não encontrado em /x/y" confirmaria o
        # que existe e o que não existe no servidor.
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "dump do Label Studio não encontrado") from None


async def _upsert_source(project_id: str, kind: str, uri: str, name: str,
                         config: dict, actor_id: str) -> str:
    """Reaproveita a origem existente para o mesmo destino.

    Reimportar o mesmo prefixo não pode criar uma segunda origem, senão o
    mesmo bucket apareceria duas vezes na tela do dataset.
    """
    row = await db.fetch_row(
        "INSERT INTO data_source (project_id, kind, uri, name, config,"
        " created_by) VALUES (%s,%s,%s,%s,%s::jsonb,%s)"
        " ON CONFLICT (project_id, kind, uri) WHERE uri IS NOT NULL"
        " DO UPDATE SET name = EXCLUDED.name RETURNING id",
        (project_id, kind, uri, name, json.dumps(config), actor_id))
    return str(row["id"])


async def _project_context(slug: str) -> dict:
    proj = await db.fetch_one(
        "SELECT id FROM project WHERE slug = %s AND archived_at IS NULL",
        (slug,))
    if proj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "projeto não encontrado")
    # Todo projeto nasce com o schema v1 na mesma transação de `create_project`:
    # existe projeto sem imagem, nunca projeto sem schema.
    schema = await db.fetch_row(
        "SELECT id, classes FROM label_schema WHERE project_id = %s"
        " ORDER BY version DESC LIMIT 1", (proj["id"],))
    return {"project_id": str(proj["id"]), "schema_id": str(schema["id"]),
            "classes": [c["name"] for c in schema["classes"]]}


@router.post("/projects/{project_slug}/imports",
             response_model=Simulacao | ImportacaoEnfileirada)
async def create_import(body: ImportIn, project_slug: Annotated[str, Path()],
                        request: Request,
                        actor: Annotated[Principal, Depends(require_role("engineer"))],
                        dry_run: bool = True):
    """Importa (ou simula) dados de uma origem para o projeto.

    Default é `dry_run=true` de propósito: importar é uma operação que mexe no
    dado de todo mundo, e o custo de conferir antes é uma requisição.
    """
    ctx = await _project_context(project_slug)
    importer = _build_importer(body)
    report = ImportReport()

    if dry_run:
        await run_import(project_id=ctx["project_id"],
                         schema_id=ctx["schema_id"],
                         schema_classes=ctx["classes"], importer=importer,
                         actor_id=actor.id,
                         class_overrides=body.class_overrides,
                         source_id=None, dry_run=True, report=report)
        return {"dry_run": True, "source": importer.describe(),
                "source_classes": importer.classes(),
                "schema_classes": ctx["classes"], "report": report.as_dict()}

    uri = (f"gs://{body.bucket.strip('/')}/{body.prefix.lstrip('/')}"
           if body.kind == "gcs_prefix" else body.sqlite_path)
    source_id = await _upsert_source(
        ctx["project_id"],
        "gcs" if body.kind == "gcs_prefix" else "import",
        uri, importer.describe(), body.model_dump(), actor.id)
    source = {"id": source_id}

    async with db.connection() as conn:
        async with conn.transaction():
            job = await db.fetch_row(
                "INSERT INTO job (project_id, type, payload, status,"
                " created_by, data_source_id, started_at, heartbeat_at)"
                " VALUES (%s,'import',%s::jsonb,'running',%s,%s,now(),now())"
                " RETURNING id",
                (ctx["project_id"], json.dumps(body.model_dump()), actor.id,
                 source["id"]), conn=conn)
            job_id = str(job["id"])

            await audit(actor, "import.start", "job", job_id,
                        project_id=ctx["project_id"],
                        after={"kind": body.kind},
                        ip=auth.client_ip(request), conn=conn)

    # Guardar a referência: sem isso o coletor pode recolher a task no meio da
    # importação e o job fica preso em 'running' para sempre.
    tarefa = asyncio.create_task(_run_job(job_id, ctx, importer, actor.id,
                                          body.class_overrides,
                                          str(source["id"])))
    _EM_ANDAMENTO.add(tarefa)
    tarefa.add_done_callback(_EM_ANDAMENTO.discard)
    # Devolve a origem criada/reaproveitada: quem chamou precisa vinculá-la a
    # um dataset, e adivinhar "a mais recente" depois seria uma corrida.
    return {"dry_run": False, "job_id": job_id, "status": "running",
            "source_id": source_id, "source": importer.describe()}


async def _run_job(job_id: str, ctx: dict, importer, actor_id: str,
                   overrides: dict, source_id: str) -> None:
    report = ImportReport()
    try:
        await svc.registrar_evento(job_id, f"Importando de {importer.describe()}")
        await run_import(project_id=ctx["project_id"],
                         schema_id=ctx["schema_id"],
                         schema_classes=ctx["classes"], importer=importer,
                         actor_id=actor_id, class_overrides=overrides,
                         source_id=source_id, dry_run=False, report=report,
                         progress_cb=lambda n: svc.registrar_progresso(job_id, n))
        await svc.concluir_job(job_id, report.as_dict())
        await svc.registrar_evento(
            job_id,
            f"Concluído: {report.samples_created} imagens novas, "
            f"{report.samples_existing} já existentes, "
            f"{report.annotations_created} anotações, "
            f"{report.skipped_total} puladas")
    except Exception as e:
        # `job.error` e os eventos são lidos com papel `viewer`. Erro do GCS ou
        # do banco cita conta de serviço, projeto e caminho interno — detalhe
        # fica no log, mesmo tratamento já aplicado na navegação de bucket.
        logger.exception("job de importação %s falhou", job_id)
        await svc.falhar_job(job_id, f"falhou: {type(e).__name__}",
                             report.as_dict())
        await svc.registrar_evento(job_id, f"Falhou ({type(e).__name__}) — "
                                           f"detalhe no log do servidor", "error")


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
