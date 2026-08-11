"""Aba Treinamento: dispara um treino sobre uma versão congelada e acompanha.

O usuário escolhe uma **versão** (imutável) do dataset e os hiperparâmetros; a
API enfileira um `job` do tipo `train` e cria o `training_run` que o vincula à
versão e ao schema. Quem executa é um agente com capacidade `train` — a API não
treina nada, só orquestra.

Dois principais no registro: `created_by` é quem pediu (pessoa), `device_id` é
quem executou (máquina). É o que permite auditar "quem disparou este treino".
"""
from __future__ import annotations

import json
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi import Path as P
from pydantic import BaseModel, Field

from .. import auth, db
from ..auth import Principal
from ..deps import audit, require_role

router = APIRouter(tags=["training"])

# Bases YOLO aceitas — restringir evita que um typo vire download silencioso de
# um peso qualquer no agente, e documenta o que a instalação suporta.
BASES = {"yolo11n.pt", "yolo11s.pt", "yolo11m.pt",
         "yolov8n.pt", "yolov8s.pt", "yolov8m.pt"}


class RunIn(BaseModel):
    dataset_slug: str = Field(min_length=1)
    version: int = Field(ge=1)
    experiment: str = Field(min_length=1, max_length=120)
    model_slug: str = Field(default="detector", min_length=2, max_length=64)
    base_model: str = "yolo11n.pt"
    epochs: int = Field(default=100, ge=1, le=1000)
    imgsz: int = Field(default=640, ge=64, le=2048)
    batch: int = Field(default=16, ge=1, le=256)
    # máquina desejada: vazio = próxima livre do pool; um slug = fixa nela
    # (o job espera essa máquina liberar, mesmo se estiver ocupada).
    device_slug: str | None = Field(default=None, max_length=64)


def _slug(texto: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", texto.strip().lower()).strip("-")
    return s or "exp"


@router.post("/projects/{project_slug}/runs",
             status_code=status.HTTP_201_CREATED)
async def criar_run(body: RunIn, project_slug: Annotated[str, P()],
                    request: Request,
                    actor: Annotated[Principal, Depends(require_role("engineer"))]):
    if body.base_model not in BASES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"base '{body.base_model}' não suportada "
                            f"({', '.join(sorted(BASES))})")

    dv = await db.fetch_one(
        "SELECT v.id, v.schema_id, v.manifest_uri, v.version,"
        "       d.slug AS dataset_slug, p.id AS project_id"
        "  FROM dataset_version v"
        "  JOIN dataset d ON d.id = v.dataset_id"
        "  JOIN project p ON p.id = d.project_id"
        " WHERE p.slug = %s AND d.slug = %s AND v.version = %s",
        (project_slug, body.dataset_slug, body.version))
    if dv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "versão de dataset não encontrada — congele antes")

    # máquina-alvo (opcional): se pedida, tem de existir, não estar desabilitada
    # e saber treinar — senão o job ficaria preso na fila para sempre. Offline é
    # permitido de propósito: o job espera a máquina voltar e liberar.
    target_device_id = None
    if body.device_slug:
        dev = await db.fetch_one(
            "SELECT id, status, capabilities FROM device WHERE slug = %s",
            (body.device_slug,))
        if dev is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"máquina '{body.device_slug}' não encontrada")
        if dev["status"] == "disabled":
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"máquina '{body.device_slug}' está desabilitada")
        if not dev["capabilities"].get("train"):
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"máquina '{body.device_slug}' não faz treino")
        target_device_id = dev["id"]

    exp_slug = _slug(body.experiment)
    params = {"epochs": body.epochs, "imgsz": body.imgsz, "batch": body.batch}

    async with db.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "INSERT INTO experiment (project_id, slug, name, created_by)"
                " VALUES (%s,%s,%s,%s)"
                " ON CONFLICT (project_id, slug) DO UPDATE SET slug = EXCLUDED.slug"
                " RETURNING id", (dv["project_id"], exp_slug, body.experiment,
                                  actor.id))
            exp = await cur.fetchone()

            # payload leva tudo que o agente precisa sem uma segunda ida ao banco;
            # requires é o contrato de roteamento: só device com 'train' pega.
            payload = {"dataset_version_id": str(dv["id"]),
                       "model_slug": body.model_slug,
                       "base_model": body.base_model, "params": params,
                       "project_slug": project_slug,
                       "dataset_slug": dv["dataset_slug"],
                       "version": dv["version"]}
            cur = await conn.execute(
                "INSERT INTO job (project_id, type, payload, requires,"
                " created_by, target_device_id)"
                " VALUES (%s,'train',%s::jsonb,%s::jsonb,%s,%s) RETURNING id",
                (dv["project_id"], json.dumps(payload),
                 json.dumps({"train": True}), actor.id, target_device_id))
            job = await cur.fetchone()

            cur = await conn.execute(
                "INSERT INTO training_run (experiment_id, job_id,"
                " dataset_version_id, schema_id, base_model_name, params,"
                " created_by) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING id",
                (exp["id"], job["id"], dv["id"], dv["schema_id"],
                 body.base_model, json.dumps(params), actor.id))
            run = await cur.fetchone()

            # o run precisa estar no payload para o agente mandar métricas por ele
            await conn.execute(
                "UPDATE job SET payload = payload || %s::jsonb WHERE id = %s",
                (json.dumps({"run_id": str(run["id"])}), job["id"]))

            await audit(actor, "run.create", "training_run", str(run["id"]),
                        project_id=str(dv["project_id"]),
                        after={"experimento": exp_slug,
                               "versao": dv["version"],
                               "base": body.base_model},
                        ip=auth.client_ip(request), conn=conn)
    return {"run_id": str(run["id"]), "job_id": str(job["id"]),
            "experimento": exp_slug, "status": "queued"}


@router.get("/train-devices",
            dependencies=[Depends(require_role("engineer"))])
async def train_devices():
    """Máquinas que o engenheiro pode escolher como alvo de um treino: capazes
    de treinar e não desabilitadas. Só nome/estado — sem capabilities nem token
    (esses ficam com o superusuário, na aba Máquinas)."""
    return await db.fetch_all(
        "SELECT slug, name, status FROM device"
        " WHERE status <> 'disabled' AND capabilities @> '{\"train\": true}'"
        " ORDER BY slug")


@router.get("/projects/{project_slug}/runs",
            dependencies=[Depends(require_role("viewer"))])
async def listar_runs(project_slug: Annotated[str, P()],
                      limite: int = Query(50, ge=1, le=200)):
    return await db.fetch_all(
        "SELECT r.id, e.slug AS experimento, r.base_model_name,"
        "       j.status AS job_status, r.status AS run_status,"
        "       r.metrics, r.best_epoch, r.started_at, r.finished_at,"
        "       r.duration_s, d.slug AS dataset, v.version AS dataset_version,"
        "       dev.slug AS device, u.display_name AS criado_por"
        "  FROM training_run r"
        "  JOIN experiment e ON e.id = r.experiment_id"
        "  JOIN job j ON j.id = r.job_id"
        "  JOIN dataset_version v ON v.id = r.dataset_version_id"
        "  JOIN dataset d ON d.id = v.dataset_id"
        "  LEFT JOIN device dev ON dev.id = j.device_id"
        "  LEFT JOIN app_user u ON u.id = r.created_by"
        " WHERE e.project_id = (SELECT id FROM project WHERE slug = %s)"
        " ORDER BY r.started_at DESC LIMIT %s", (project_slug, limite))


async def _run(project_slug: str, run_id: str) -> dict:
    row = await db.fetch_one(
        "SELECT r.id, r.job_id, j.status AS job_status, j.error AS job_error,"
        "       j.progress, r.status AS run_status, r.metrics, r.best_epoch,"
        "       r.params, r.base_model_name, r.started_at, r.finished_at,"
        "       r.duration_s, e.slug AS experimento, d.slug AS dataset,"
        "       v.version AS dataset_version, dev.slug AS device,"
        "       alvo.slug AS target_device"
        "  FROM training_run r"
        "  JOIN experiment e ON e.id = r.experiment_id"
        "  JOIN job j ON j.id = r.job_id"
        "  JOIN dataset_version v ON v.id = r.dataset_version_id"
        "  JOIN dataset d ON d.id = v.dataset_id"
        "  LEFT JOIN device dev ON dev.id = j.device_id"
        "  LEFT JOIN device alvo ON alvo.id = j.target_device_id"
        " WHERE e.project_id = (SELECT id FROM project WHERE slug = %s)"
        "   AND r.id = %s", (project_slug, run_id))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run não encontrado")
    return row


@router.get("/projects/{project_slug}/runs/{run_id}",
            dependencies=[Depends(require_role("viewer"))])
async def detalhe_run(project_slug: Annotated[str, P()], run_id: str):
    run = await _run(project_slug, run_id)
    # a versão de modelo que este treino produziu, se já registrada
    mv = await db.fetch_one(
        "SELECT mv.id, m.slug AS model_slug, mv.version, mv.metrics,"
        "       mv.weights_uri, mv.bytes, mv.created_at"
        "  FROM model_version mv JOIN model m ON m.id = mv.model_id"
        " WHERE mv.run_id = %s", (run_id,))
    return {**run, "model_version": mv}


@router.get("/projects/{project_slug}/runs/{run_id}/stream",
            dependencies=[Depends(require_role("viewer"))])
async def stream_run(project_slug: Annotated[str, P()], run_id: str,
                     desde_seq: int = Query(0, ge=0),
                     desde_epoca: int = Query(-1, ge=-1)):
    """Leitura incremental para a tela ao vivo: eventos após `desde_seq` e
    métricas após `desde_epoca`. O cliente guarda os dois cursores e só pede o
    que falta — nada de reler o log inteiro a cada atualização."""
    run = await _run(project_slug, run_id)
    eventos = await db.fetch_all(
        "SELECT seq, at, level, message FROM job_event"
        " WHERE job_id = %s AND seq > %s ORDER BY seq LIMIT 500",
        (run["job_id"], desde_seq))
    metricas = await db.fetch_all(
        "SELECT epoch, key, value FROM run_metric"
        " WHERE run_id = %s AND epoch > %s ORDER BY epoch, key",
        (run_id, desde_epoca))
    return {"job_status": run["job_status"], "run_status": run["run_status"],
            "progress": run["progress"], "error": run["job_error"],
            "metrics_finais": run["metrics"], "best_epoch": run["best_epoch"],
            "eventos": eventos, "metricas": metricas}


@router.post("/projects/{project_slug}/runs/{run_id}/cancel")
async def cancelar_run(project_slug: Annotated[str, P()], run_id: str,
                       request: Request,
                       actor: Annotated[Principal, Depends(require_role("engineer"))]):
    """Pede cancelamento. Se ainda na fila, cancela na hora; se já rodando,
    marca a flag e o agente para no próximo heartbeat."""
    run = await _run(project_slug, run_id)
    if run["job_status"] in ("succeeded", "failed", "canceled"):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"job já terminou ({run['job_status']})")
    async with db.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "UPDATE job SET cancel_requested = true,"
                "  status = CASE WHEN status = 'queued' THEN 'canceled'"
                "                ELSE status END,"
                "  finished_at = CASE WHEN status = 'queued' THEN now()"
                "                     ELSE finished_at END"
                " WHERE id = %s RETURNING status", (run["job_id"],))
            j = await cur.fetchone()
            if j["status"] == "canceled":
                await conn.execute(
                    "UPDATE training_run SET status = 'stopped',"
                    " finished_at = now() WHERE id = %s", (run_id,))
            await audit(actor, "run.cancel", "training_run", run_id,
                        ip=auth.client_ip(request), conn=conn)
    return {"run_id": run_id, "status": j["status"]}
