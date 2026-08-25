"""Treino: enfileirar um run sobre uma versão congelada e acompanhá-lo.

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
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, status

from contracts import jobs

from .. import db
from ..audit import audit
from ..auth import Principal
from .jobs import TERMINAIS

# Bases YOLO aceitas — restringir evita que um typo vire download silencioso de
# um peso qualquer no agente, e documenta o que a instalação suporta.
BASES = {"yolo11n.pt", "yolo11s.pt", "yolo11m.pt",
         "yolov8n.pt", "yolov8s.pt", "yolov8m.pt"}


def slug_experimento(texto: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", texto.strip().lower()).strip("-")
    return s or "exp"


async def criar_run(project_slug: str, actor: Principal, *, dataset_slug: str,
                    version: int, experiment: str, model_slug: str,
                    base_model: str, params: dict, device_slug: str | None,
                    ip: str | None) -> dict:
    if base_model not in BASES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"base '{base_model}' não suportada "
                            f"({', '.join(sorted(BASES))})")

    exp_slug = slug_experimento(experiment)
    async with db.connection() as conn, conn.transaction():
        dv = await db.fetch_one(
            "SELECT v.id, v.schema_id, v.manifest_uri, v.version,"
            "       d.slug AS dataset_slug, p.id AS project_id"
            "  FROM dataset_version v"
            "  JOIN dataset d ON d.id = v.dataset_id"
            "  JOIN project p ON p.id = d.project_id"
            " WHERE p.slug = %s AND d.slug = %s AND v.version = %s",
            (project_slug, dataset_slug, version), conn=conn)
        if dv is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "versão de dataset não encontrada — congele antes")
        target_device_id = await _maquina_alvo(conn, device_slug)

        exp = await db.fetch_row(
            "INSERT INTO experiment (project_id, slug, name, created_by)"
            " VALUES (%s,%s,%s,%s)"
            " ON CONFLICT (project_id, slug) DO UPDATE SET slug = EXCLUDED.slug"
            " RETURNING id",
            (dv["project_id"], exp_slug, experiment, actor.id), conn=conn)

        # O id do run é sorteado aqui, e não pelo banco, para caber no payload
        # do job: o run tem FK para o job, então ele só pode ser inserido
        # depois — e sem o id em mãos o payload precisaria de um UPDATE
        # posterior para ganhar a chave por onde o agente reporta.
        run_id = str(uuid4())
        # payload leva tudo que o agente precisa sem uma segunda ida ao banco;
        # requires é o contrato de roteamento: só device com 'train' pega.
        payload = jobs.Train(
            dataset_version_id=str(dv["id"]), run_id=run_id,
            model_slug=model_slug, base_model=base_model, params=params,
            project_slug=project_slug, dataset_slug=dv["dataset_slug"],
            version=dv["version"])
        job = await db.fetch_row(
            "INSERT INTO job (project_id, type, payload, requires,"
            " created_by, target_device_id)"
            " VALUES (%s,'train',%s::jsonb,%s::jsonb,%s,%s) RETURNING id",
            (dv["project_id"], json.dumps(payload.como_dict()),
             json.dumps({"train": True}), actor.id, target_device_id),
            conn=conn)

        run = await db.fetch_row(
            "INSERT INTO training_run (id, experiment_id, job_id,"
            " dataset_version_id, schema_id, base_model_name, params,"
            " created_by) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)"
            " RETURNING id",
            (run_id, exp["id"], job["id"], dv["id"], dv["schema_id"],
             base_model, json.dumps(params), actor.id), conn=conn)

        await audit(actor, "run.create", "training_run", str(run["id"]),
                    project_id=str(dv["project_id"]),
                    after={"experimento": exp_slug, "versao": dv["version"],
                           "base": base_model}, ip=ip, conn=conn)
    return {"run_id": str(run["id"]), "job_id": str(job["id"]),
            "experimento": exp_slug, "status": "queued"}


async def _maquina_alvo(conn: db.Conn,
                        device_slug: str | None) -> str | None:
    """Máquina-alvo (opcional): se pedida, tem de existir, não estar
    desabilitada e saber treinar — senão o job ficaria preso na fila para
    sempre. Offline é permitido de propósito: o job espera a máquina voltar e
    liberar."""
    if not device_slug:
        return None
    dev = await db.fetch_one(
        "SELECT id, status, capabilities FROM device WHERE slug = %s",
        (device_slug,), conn=conn)
    if dev is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"máquina '{device_slug}' não encontrada")
    if dev["status"] == "disabled":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"máquina '{device_slug}' está desabilitada")
    if not dev["capabilities"].get("train"):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"máquina '{device_slug}' não faz treino")
    return dev["id"]


async def maquinas_de_treino() -> list[dict[str, Any]]:
    """Máquinas que o engenheiro pode escolher como alvo de um treino: capazes
    de treinar e não desabilitadas. Só nome/estado — sem capabilities nem token
    (esses ficam com o superusuário, na aba Máquinas)."""
    async with db.connection() as conn:
        return await db.fetch_all(
            "SELECT slug, name, status FROM device"
            " WHERE status <> 'disabled'"
            "   AND capabilities @> '{\"train\": true}' ORDER BY slug",
            conn=conn)


async def listar_runs(project_slug: str, limite: int) -> list[dict[str, Any]]:
    async with db.connection() as conn:
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
            " ORDER BY r.started_at DESC LIMIT %s",
            (project_slug, limite), conn=conn)


async def buscar_run(conn: db.Conn, project_slug: str,
                     run_id: UUID) -> dict:
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
        "   AND r.id = %s", (project_slug, run_id), conn=conn)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run não encontrado")
    return row


async def detalhe_run(project_slug: str, run_id: UUID) -> dict:
    async with db.connection() as conn:
        run = await buscar_run(conn, project_slug, run_id)
        # a versão de modelo que este treino produziu, se já registrada
        mv = await db.fetch_one(
            "SELECT mv.id, m.slug AS model_slug, mv.version, mv.metrics,"
            "       mv.weights_uri, mv.bytes, mv.created_at"
            "  FROM model_version mv JOIN model m ON m.id = mv.model_id"
            " WHERE mv.run_id = %s", (run_id,), conn=conn)
    return {**run, "model_version": mv}


async def stream_run(project_slug: str, run_id: UUID, *, desde_seq: int,
                     desde_epoca: int) -> dict:
    """Leitura incremental para a tela ao vivo: eventos após `desde_seq` e
    métricas após `desde_epoca`. O cliente guarda os dois cursores e só pede o
    que falta — nada de reler o log inteiro a cada atualização."""
    async with db.connection() as conn:
        run = await buscar_run(conn, project_slug, run_id)
        eventos = await db.fetch_all(
            "SELECT seq, at, level, message FROM job_event"
            " WHERE job_id = %s AND seq > %s ORDER BY seq LIMIT 500",
            (run["job_id"], desde_seq), conn=conn)
        metricas = await db.fetch_all(
            "SELECT epoch, key, value FROM run_metric"
            " WHERE run_id = %s AND epoch > %s ORDER BY epoch, key",
            (run_id, desde_epoca), conn=conn)
    return {"job_status": run["job_status"], "run_status": run["run_status"],
            "progress": run["progress"], "error": run["job_error"],
            "metrics_finais": run["metrics"], "best_epoch": run["best_epoch"],
            "eventos": eventos, "metricas": metricas}


async def cancelar_run(project_slug: str, run_id: UUID, actor: Principal, *,
                       ip: str | None) -> dict:
    """Pede cancelamento. Se ainda na fila, cancela na hora; se já rodando,
    marca a flag e o agente para no próximo heartbeat."""
    async with db.connection() as conn, conn.transaction():
        run = await buscar_run(conn, project_slug, run_id)
        # A guarda de estado terminal vive no `WHERE`, e não numa leitura
        # anterior: entre ler e atualizar cabe o job terminar, e aí o 409 não
        # sai e o UPDATE marca `cancel_requested` num treino já encerrado.
        # Mesmo desenho de `services/jobs.py: cancelar`.
        j = await db.fetch_one(
            "UPDATE job SET cancel_requested = true,"
            "  status = CASE WHEN status = 'queued' THEN 'canceled'"
            "                ELSE status END,"
            "  finished_at = CASE WHEN status = 'queued' THEN now()"
            "                     ELSE finished_at END"
            " WHERE id = %s AND status <> ALL(%s::text[]) RETURNING status",
            (run["job_id"], list(TERMINAIS)), conn=conn)
        if j is None:
            # Relê para a mensagem: `run["job_status"]` é de antes do UPDATE, e
            # justamente na corrida que a guarda pega ele ainda diz `running` —
            # "job já terminou (running)" se contradiz na cara de quem lê.
            atual = await db.fetch_row(
                "SELECT status FROM job WHERE id = %s", (run["job_id"],),
                conn=conn)
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"job já terminou ({atual['status']})")
        if j["status"] == "canceled":
            await db.execute(
                "UPDATE training_run SET status = 'stopped',"
                " finished_at = now() WHERE id = %s", (run_id,), conn=conn)
        await audit(actor, "run.cancel", "training_run", str(run_id), ip=ip,
                    conn=conn)
    return {"run_id": run_id, "status": j["status"]}
