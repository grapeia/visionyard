"""Protocolo do agente: como uma máquina reclama e executa um job.

Autenticação por `X-Agent-Token` (device), nunca pela porta de usuário. O ciclo
que o agente roda:

    register → claim → (manifest) → heartbeat/events/metrics* → model → finish

O *claim* é uma corrida entre agentes resolvida no banco: `FOR UPDATE SKIP
LOCKED` garante que dois agentes nunca peguem o mesmo job, e `capabilities @>
requires` garante que só uma máquina compatível o pegue (uma máquina de GPU pega
`train`, um dispositivo de inferência não). Job cujo dono morreu — heartbeat
velho — volta a ser reclamável, para que um reboot não deixe treino preso para
sempre.
"""
from __future__ import annotations

import json
import math
from pathlib import Path as FsPath
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import db
from ..config import settings
from ..deps import CurrentDevice

router = APIRouter(tags=["agents"], prefix="/agent")


# --------------------------------------------------------------------------- #
# Presença
# --------------------------------------------------------------------------- #

# Chaves que o agente pode declarar: só DIAGNÓSTICO. As flags de roteamento
# (train/infer) são do admin — se o agente pudesse mesclá-las, um dispositivo
# cadastrado como `infer` se declararia `train` e passaria a reclamar treino,
# furando o controle de quem executa o quê. jsonb `||` faz a direita vencer,
# então filtrar a entrada é o que impede a escalada (não a ordem do merge).
CAPS_DIAGNOSTICO = {"gpu", "cuda", "vram_gb", "jetpack", "dla", "trt",
                    "torch", "host", "os", "driver", "arch"}


class RegisterIn(BaseModel):
    capabilities: dict = Field(default_factory=dict)
    agent_version: str | None = None


@router.post("/register")
async def register(body: RegisterIn, dev: CurrentDevice):
    diag = {k: v for k, v in body.capabilities.items() if k in CAPS_DIAGNOSTICO}
    await db.execute(
        "UPDATE device SET capabilities = capabilities || %s::jsonb,"
        "  status = CASE WHEN status = 'disabled' THEN status ELSE 'idle' END,"
        "  last_heartbeat = now() WHERE id = %s",
        (json.dumps(diag), dev.id))
    return {"device_id": dev.id, "slug": dev.slug}


class HeartbeatIn(BaseModel):
    status: Literal["idle", "busy"] = "idle"


@router.post("/heartbeat")
async def heartbeat(body: HeartbeatIn, dev: CurrentDevice):
    await db.execute(
        "UPDATE device SET last_heartbeat = now(),"
        "  status = CASE WHEN status IN ('disabled','draining') THEN status"
        "                ELSE %s END WHERE id = %s", (body.status, dev.id))
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Claim
# --------------------------------------------------------------------------- #

@router.post("/claim")
async def claim(dev: CurrentDevice):
    """Reclama o próximo job compatível; `{"job": null}` quando não há nada para
    esta máquina. `draining`/`disabled` não reclamam — só terminam o que já
    pegaram, para o dispositivo de produção poder sair do pool sem largar um job no
    meio."""
    stale = settings.agent_job_stale_seconds
    d = await db.fetch_one("SELECT status FROM device WHERE id = %s", (dev.id,))
    if d["status"] in ("draining", "disabled"):
        return {"job": None}

    # Reaper: job órfão que NÃO pode mais ser reclamado (agente morreu no limite
    # de tentativas, ou foi cancelado com o agente já fora do ar) viraria preso
    # em 'running' para sempre — o claim abaixo só reclama attempts<max e
    # !cancel_requested. Aqui ele é levado a um estado terminal.
    await db.execute(
        "WITH mortos AS ("
        "  UPDATE job SET"
        "    status = CASE WHEN cancel_requested THEN 'canceled' ELSE 'failed' END,"
        "    error = COALESCE(error, 'agente parou de responder'),"
        "    finished_at = now()"
        "  WHERE status IN ('claimed','running')"
        "    AND heartbeat_at < now() - make_interval(secs => %(s)s)"
        "    AND (attempts >= max_attempts OR cancel_requested)"
        "  RETURNING id, status)"
        " UPDATE training_run r"
        "    SET status = CASE WHEN m.status = 'canceled' THEN 'stopped'"
        "                      ELSE 'failed' END, finished_at = now()"
        "   FROM mortos m WHERE r.job_id = m.id AND r.status = 'running'",
        {"s": stale})

    async with db.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                """
                UPDATE job SET status = 'claimed', device_id = %(dev)s,
                       claimed_at = now(), heartbeat_at = now(),
                       attempts = attempts + 1, error = NULL
                 WHERE id = (
                    SELECT j.id FROM job j
                     WHERE (SELECT capabilities FROM device WHERE id = %(dev)s)
                           @> j.requires
                       AND (j.target_device_id IS NULL
                            OR j.target_device_id = %(dev)s)
                       AND NOT j.cancel_requested
                       AND (
                         j.status = 'queued'
                         OR (j.status IN ('claimed','running')
                             AND j.heartbeat_at
                                 < now() - make_interval(secs => %(stale)s)
                             AND j.attempts < j.max_attempts)
                       )
                     ORDER BY (j.status = 'queued') DESC, j.priority, j.created_at
                     FOR UPDATE SKIP LOCKED
                     LIMIT 1)
                RETURNING id, type, payload, project_id, attempts
                """, {"dev": dev.id, "stale": stale})
            job = await cur.fetchone()
            if job is None:
                return {"job": None}
            await conn.execute(
                "UPDATE device SET status = 'busy' WHERE id = %s", (dev.id,))
    return {"job": {"id": str(job["id"]), "type": job["type"],
                    "payload": job["payload"], "attempt": job["attempts"]}}


async def _own_job(job_id: str, dev: CurrentDevice) -> dict:
    """Job que ESTA máquina reclamou — outra não mexe no job alheio."""
    row = await db.fetch_one(
        "SELECT id, type, payload, status, device_id, cancel_requested,"
        "       project_id FROM job WHERE id = %s", (job_id,))
    if row is None or str(row["device_id"]) != dev.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job não é desta máquina")
    return row


# --------------------------------------------------------------------------- #
# Manifesto (só quem é dono do job de treino lê)
# --------------------------------------------------------------------------- #

async def _manifest_dir(job_id: str, dev: CurrentDevice) -> FsPath:
    job = await _own_job(job_id, dev)
    dvid = job["payload"].get("dataset_version_id")
    row = await db.fetch_one(
        "SELECT manifest_uri FROM dataset_version WHERE id = %s", (dvid,))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "versão sem manifesto")
    return FsPath(row["manifest_uri"])


@router.get("/jobs/{job_id}/manifest")
async def manifest_meta(job_id: Annotated[str, Path()], dev: CurrentDevice):
    """meta.json: classes, split e estatísticas. Pequeno — vai como JSON."""
    d = await _manifest_dir(job_id, dev)
    meta = d / "meta.json"
    if not meta.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "meta.json ausente")
    return json.loads(meta.read_text())


@router.get("/jobs/{job_id}/manifest/items")
async def manifest_items(job_id: Annotated[str, Path()], dev: CurrentDevice):
    """items.jsonl: uma linha por imagem (uri, split, boxes). Pode passar de 100
    MB — vai como arquivo, o agente lê em streaming."""
    d = await _manifest_dir(job_id, dev)
    items = d / "items.jsonl"
    if not items.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "items.jsonl ausente")
    return FileResponse(items, media_type="application/x-ndjson")


# --------------------------------------------------------------------------- #
# Progresso
# --------------------------------------------------------------------------- #

class JobHeartbeatIn(BaseModel):
    progress: dict = Field(default_factory=dict)   # {"epoch": 12, "de": 100}


@router.post("/jobs/{job_id}/heartbeat")
async def job_heartbeat(body: JobHeartbeatIn, job_id: Annotated[str, Path()],
                        dev: CurrentDevice):
    """Bate ponto do job. A resposta diz se foi pedido cancelamento — é como o
    agente sabe que precisa parar (a rota de usuário só levanta a flag)."""
    row = await db.fetch_one(
        "UPDATE job SET heartbeat_at = now(), progress = %s::jsonb,"
        "  status = CASE WHEN status = 'claimed' THEN 'running' ELSE status END,"
        "  started_at = COALESCE(started_at, now())"
        " WHERE id = %s AND device_id = %s RETURNING status, cancel_requested",
        (json.dumps(body.progress), job_id, dev.id))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job não é desta máquina")
    return {"cancel": row["cancel_requested"], "status": row["status"]}


class Evento(BaseModel):
    seq: int = Field(ge=1)
    level: Literal["debug", "info", "warn", "error"] = "info"
    message: str


class EventosIn(BaseModel):
    eventos: list[Evento]


@router.post("/jobs/{job_id}/events")
async def push_eventos(body: EventosIn, job_id: Annotated[str, Path()],
                       dev: CurrentDevice):
    await _own_job(job_id, dev)
    async with db.connection() as conn:
        async with conn.transaction():
            for e in body.eventos:
                # idempotente: reenvio após timeout não duplica linha
                await conn.execute(
                    "INSERT INTO job_event (job_id, seq, level, message)"
                    " VALUES (%s,%s,%s,%s) ON CONFLICT (job_id, seq) DO NOTHING",
                    (job_id, e.seq, e.level, e.message))
    return {"gravados": len(body.eventos)}


class Metrica(BaseModel):
    epoch: int = Field(ge=0)
    key: str = Field(min_length=1, max_length=64)
    value: float


class MetricasIn(BaseModel):
    metricas: list[Metrica]


@router.post("/jobs/{job_id}/metrics")
async def push_metricas(body: MetricasIn, job_id: Annotated[str, Path()],
                        dev: CurrentDevice):
    await _own_job(job_id, dev)
    run = await db.fetch_one(
        "SELECT id FROM training_run WHERE job_id = %s", (job_id,))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run não encontrado")
    # NaN/inf não são JSON válido: se um entrar no banco, a tela do run quebra
    # ao ler de volta. O agente já filtra, mas a API não confia no cliente.
    finitas = [m for m in body.metricas if math.isfinite(m.value)]
    async with db.connection() as conn:
        async with conn.transaction():
            for m in finitas:
                await conn.execute(
                    "INSERT INTO run_metric (run_id, epoch, key, value)"
                    " VALUES (%s,%s,%s,%s)"
                    " ON CONFLICT (run_id, epoch, key) DO UPDATE"
                    "   SET value = EXCLUDED.value, at = now()",
                    (run["id"], m.epoch, m.key, m.value))
            # melhor época pela mAP50-95: mantém training_run legível sem
            # reprocessar run_metric a cada leitura da tela.
            cur = await conn.execute(
                "SELECT epoch, value FROM run_metric"
                " WHERE run_id = %s AND key = 'mAP50-95'"
                " ORDER BY value DESC, epoch LIMIT 1", (run["id"],))
            melhor = await cur.fetchone()
            if melhor is not None:
                cur = await conn.execute(
                    "SELECT key, value FROM run_metric"
                    " WHERE run_id = %s AND epoch = %s",
                    (run["id"], melhor["epoch"]))
                resumo = {r["key"]: r["value"] for r in await cur.fetchall()}
                await conn.execute(
                    "UPDATE training_run SET metrics = %s::jsonb,"
                    " best_epoch = %s WHERE id = %s",
                    (json.dumps(resumo), melhor["epoch"], run["id"]))
    return {"gravadas": len(finitas)}


# --------------------------------------------------------------------------- #
# Pré-tagueamento: alvos e escrita das predições
# --------------------------------------------------------------------------- #

@router.get("/jobs/{job_id}/targets")
async def prelabel_targets(job_id: Annotated[str, Path()], dev: CurrentDevice):
    """Imagens que este job de pré-tag deve prever: as do dataset do escopo, e
    (se only_missing) só as que ainda não têm anotação de quem disparou."""
    job = await _own_job(job_id, dev)
    p = job["payload"]
    autor = await db.fetch_one("SELECT created_by FROM job WHERE id = %s",
                               (job_id,))
    if p.get("sample_id"):
        # escopo de uma imagem só (tecla w): sem filtro de only_missing.
        rows = await db.fetch_all(
            "SELECT id, uri FROM sample WHERE id = %s AND project_id = %s",
            (p["sample_id"], job["project_id"]))
    elif p.get("sample_ids"):
        # lote específico (ex.: só a página aberta): respeita only_missing.
        rows = await db.fetch_all(
            "SELECT s.id, s.uri FROM sample s"
            " WHERE s.project_id = %(p)s AND s.id = ANY(%(ids)s::uuid[])"
            "   AND (NOT %(mo)s OR NOT EXISTS ("
            "        SELECT 1 FROM annotation a"
            "         WHERE a.sample_id = s.id AND a.author_id = %(au)s))"
            " ORDER BY s.id",
            {"p": job["project_id"], "ids": p["sample_ids"],
             "mo": bool(p.get("only_missing", True)),
             "au": autor["created_by"]})
    else:
        rows = await db.fetch_all(
            """
            SELECT DISTINCT s.id, s.uri FROM sample s
              JOIN sample_source ss ON ss.sample_id = s.id
              JOIN dataset_source dsc ON dsc.data_source_id = ss.data_source_id
              JOIN dataset d ON d.id = dsc.dataset_id
             WHERE d.slug = %(ds)s AND d.project_id = %(p)s
               AND (NOT %(mo)s OR NOT EXISTS (
                    SELECT 1 FROM annotation a
                     WHERE a.sample_id = s.id AND a.author_id = %(au)s))
             ORDER BY s.id
            """, {"ds": p.get("dataset_slug"), "p": job["project_id"],
                  "mo": bool(p.get("only_missing", True)),
                  "au": autor["created_by"]})
    return {"conf": p.get("conf", 0.25),
            "alvos": [{"sample_id": str(r["id"]), "uri": r["uri"]} for r in rows]}


class PreCaixa(BaseModel):
    cls: int = Field(ge=0)
    cx: float = Field(ge=0, le=1)
    cy: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)
    conf: float | None = None


class Predicao(BaseModel):
    sample_id: str
    boxes: list[PreCaixa]


class PrelabelIn(BaseModel):
    predicoes: list[Predicao]


@router.post("/jobs/{job_id}/prelabel")
async def prelabel_gravar(body: PrelabelIn, job_id: Annotated[str, Path()],
                          dev: CurrentDevice):
    """Grava as caixas previstas como anotação `prelabel` do autor do job.

    Nunca toca em trabalho humano: o UPDATE só acontece se a anotação existente
    também for um prelabel (re-rodar atualiza a sugestão). Predição vazia é
    ignorada — a imagem segue pendente para um humano."""
    job = await _own_job(job_id, dev)
    meta = await db.fetch_one(
        "SELECT created_by FROM job WHERE id = %s", (job_id,))
    autor = meta["created_by"]
    schema_id = job["payload"].get("schema_id")
    gravadas = 0
    async with db.connection() as conn:
        async with conn.transaction():
            for pred in body.predicoes:
                if not pred.boxes:
                    continue
                caixas = [b.model_dump() for b in pred.boxes]
                # SELECT do sample restringe a escrita ao projeto do job: sem
                # isto um agente com um job legítimo do projeto A poderia injetar
                # prelabels em imagens do projeto B (o sample_id vem do corpo).
                cur = await conn.execute(
                    "INSERT INTO annotation (sample_id, schema_id, boxes, origin,"
                    " status, author_id, updated_by)"
                    " SELECT s.id, %s, %s::jsonb, 'prelabel', 'draft', %s, %s"
                    "   FROM sample s WHERE s.id = %s AND s.project_id = %s"
                    " ON CONFLICT (sample_id, author_id) DO UPDATE"
                    "    SET boxes = EXCLUDED.boxes, updated_at = now(),"
                    "        updated_by = EXCLUDED.updated_by"
                    "  WHERE annotation.origin = 'prelabel'"
                    " RETURNING id, (xmax = 0) AS novo",
                    (schema_id, json.dumps(caixas), autor, autor,
                     pred.sample_id, job["project_id"]))
                row = await cur.fetchone()
                if row is not None:
                    gravadas += 1
                    # origin='prelabel' já marca que é do modelo; a ação no
                    # histórico fica no vocabulário permitido (create/update).
                    await conn.execute(
                        "INSERT INTO annotation_history (annotation_id, sample_id,"
                        " actor_id, action, boxes_after)"
                        " VALUES (%s,%s,%s,%s,%s::jsonb)",
                        (row["id"], pred.sample_id, autor,
                         "create" if row["novo"] else "update",
                         json.dumps(caixas)))
    return {"gravadas": gravadas, "recebidas": len(body.predicoes)}


# --------------------------------------------------------------------------- #
# Registro do modelo produzido
# --------------------------------------------------------------------------- #

class ModelIn(BaseModel):
    weights_uri: str = Field(min_length=1)   # gs://… onde o agente subiu o .pt
    sha256: str = Field(min_length=64, max_length=64)
    bytes: int | None = None
    metrics: dict = Field(default_factory=dict)


@router.post("/jobs/{job_id}/model")
async def registrar_modelo(body: ModelIn, job_id: Annotated[str, Path()],
                           dev: CurrentDevice):
    """Cria uma versão imutável do modelo a partir do peso que o agente já subiu
    ao bucket. É INSERT único: model_version não aceita UPDATE (008)."""
    job = await _own_job(job_id, dev)
    run = await db.fetch_one(
        "SELECT id, schema_id, created_by FROM training_run WHERE job_id = %s",
        (job_id,))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run não encontrado")
    slug = job["payload"].get("model_slug", "detector")
    async with db.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                               (f"model:{job['project_id']}:{slug}",))
            cur = await conn.execute(
                "SELECT id FROM model WHERE project_id = %s AND slug = %s",
                (job["project_id"], slug))
            m = await cur.fetchone()
            if m is None:
                # herda o task_type do projeto — um projeto segment/classify não
                # deve criar modelo 'detect' fixo.
                cur = await conn.execute(
                    "INSERT INTO model (project_id, slug, name, task_type,"
                    " created_by)"
                    " SELECT %s,%s,%s, task_type, %s FROM project WHERE id = %s"
                    " RETURNING id",
                    (job["project_id"], slug, slug, run["created_by"],
                     job["project_id"]))
                m = await cur.fetchone()
            cur = await conn.execute(
                "SELECT COALESCE(max(version),0)+1 AS v FROM model_version"
                " WHERE model_id = %s", (m["id"],))
            versao = (await cur.fetchone())["v"]
            cur = await conn.execute(
                "INSERT INTO model_version (model_id, version, run_id, schema_id,"
                " weights_uri, sha256, bytes, metrics, status)"
                " VALUES (%s,%s,%s,%s,%s,decode(%s,'hex'),%s,%s::jsonb,'ready')"
                " RETURNING id",
                (m["id"], versao, run["id"], run["schema_id"], body.weights_uri,
                 body.sha256, body.bytes, json.dumps(body.metrics)))
            mv = await cur.fetchone()
    return {"model_slug": slug, "version": versao,
            "model_version_id": str(mv["id"])}


# --------------------------------------------------------------------------- #
# Build (artefato implantável: onnx/engine)
# --------------------------------------------------------------------------- #

class BuildIn(BaseModel):
    format: Literal["pt", "onnx", "engine"]
    uri: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    bytes: int | None = None
    # precision, imgsz, dla, jetpack, trt — o device_class a API acrescenta
    target: dict = Field(default_factory=dict)
    bench: dict = Field(default_factory=dict)   # latência p50/p95 medida no alvo


@router.post("/jobs/{job_id}/build")
async def registrar_build(body: BuildIn, job_id: Annotated[str, Path()],
                          dev: CurrentDevice):
    """Registra o artefato compilado. O device_class entra no alvo aqui — é a
    máquina que compilou que define para qual classe o engine vale."""
    job = await _own_job(job_id, dev)
    mvid = job["payload"].get("model_version_id")
    if mvid is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job sem model_version")
    d = await db.fetch_one(
        "SELECT device_class, capabilities FROM device WHERE id = %s", (dev.id,))
    alvo = {**body.target, "device_class": d["device_class"]}
    meta = await db.fetch_one("SELECT created_by FROM job WHERE id = %s", (job_id,))
    row = await db.fetch_one(
        "INSERT INTO model_build (model_version_id, format, target, uri, sha256,"
        " bytes, bench, built_by_job_id, built_on_device, created_by)"
        " VALUES (%s,%s,%s::jsonb,%s,decode(%s,'hex'),%s,%s::jsonb,%s,%s,%s)"
        " ON CONFLICT (model_version_id, format, target) DO NOTHING"
        " RETURNING id",
        (mvid, body.format, json.dumps(alvo), body.uri, body.sha256, body.bytes,
         json.dumps(body.bench), job_id, dev.id, meta["created_by"]))
    if row is None:
        return {"ok": True, "ja_existia": True}
    return {"ok": True, "build_id": str(row["id"]), "target": alvo}


# --------------------------------------------------------------------------- #
# Avaliação (medida contra uma versão de benchmark)
# --------------------------------------------------------------------------- #

class EvaluationIn(BaseModel):
    metrics: dict = Field(default_factory=dict)
    artifacts_uri: str | None = None


@router.post("/jobs/{job_id}/evaluation")
async def registrar_avaliacao(body: EvaluationIn, job_id: Annotated[str, Path()],
                              dev: CurrentDevice):
    """Registra a métrica do modelo naquele benchmark. Imutável — reavaliar cria
    outra linha; a linhagem (qual versão, qual benchmark) fica gravada."""
    job = await _own_job(job_id, dev)
    p = job["payload"]
    mvid, dvid = p.get("model_version_id"), p.get("dataset_version_id")
    if not (mvid and dvid):
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "job de avaliação sem model_version/dataset_version")
    meta = await db.fetch_one("SELECT created_by FROM job WHERE id = %s", (job_id,))
    # model_build_id distingue os artefatos (pt vs engine vs DLA) na mesma
    # versão+benchmark — é o que a comparação usa.
    row = await db.fetch_one(
        "INSERT INTO evaluation (model_version_id, model_build_id,"
        " dataset_version_id, job_id, metrics, artifacts_uri, created_by)"
        " VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s) RETURNING id",
        (mvid, p.get("model_build_id"), dvid, job_id, json.dumps(body.metrics),
         body.artifacts_uri, meta["created_by"]))
    return {"evaluation_id": str(row["id"]), "metrics": body.metrics}


# --------------------------------------------------------------------------- #
# Deployment: o device puxa o build e ativa
# --------------------------------------------------------------------------- #

@router.get("/deployments/pending")
async def deployments_pendentes(dev: CurrentDevice):
    """Implantações pendentes PARA ESTA máquina. Device desativado não recebe."""
    d = await db.fetch_one("SELECT status FROM device WHERE id = %s", (dev.id,))
    if d["status"] == "disabled":
        return {"deployments": []}
    rows = await db.fetch_all(
        "SELECT dep.id, mb.uri, mb.format, encode(mb.sha256,'hex') AS sha256,"
        "       m.slug AS model_slug, mv.version"
        "  FROM deployment dep"
        "  JOIN model_build mb ON mb.id = dep.model_build_id"
        "  JOIN model_version mv ON mv.id = mb.model_version_id"
        "  JOIN model m ON m.id = mv.model_id"
        " WHERE dep.device_id = %s AND dep.state = 'pending'"
        " ORDER BY dep.requested_at", (dev.id,))
    return {"deployments": [dict(r) for r in rows]}


class DeployState(BaseModel):
    state: Literal["active", "failed"]
    error: str | None = None


@router.post("/deployments/{dep_id}/state")
async def deployment_estado(body: DeployState, dep_id: Annotated[str, Path()],
                            dev: CurrentDevice):
    """O agente reporta o resultado. Ativar supersede a implantação ativa
    anterior no mesmo device — uma máquina roda um modelo por vez."""
    dep = await db.fetch_one(
        "SELECT id, device_id FROM deployment WHERE id = %s", (dep_id,))
    if dep is None or str(dep["device_id"]) != dev.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "deployment não é desta máquina")
    async with db.connection() as conn:
        async with conn.transaction():
            if body.state == "active":
                await conn.execute(
                    "UPDATE deployment SET state = 'superseded', ended_at = now()"
                    " WHERE device_id = %s AND state = 'active' AND id <> %s",
                    (dev.id, dep_id))
                await conn.execute(
                    "UPDATE deployment SET state = 'active', activated_at = now(),"
                    " error = NULL WHERE id = %s", (dep_id,))
            else:
                await conn.execute(
                    "UPDATE deployment SET state = 'failed', ended_at = now(),"
                    " error = %s WHERE id = %s", (body.error, dep_id))
    return {"deployment_id": dep_id, "state": body.state}


# --------------------------------------------------------------------------- #
# Fim
# --------------------------------------------------------------------------- #

class FinishIn(BaseModel):
    status: Literal["succeeded", "failed", "canceled"]
    error: str | None = None
    result: dict = Field(default_factory=dict)


@router.post("/jobs/{job_id}/finish")
async def finish(body: FinishIn, job_id: Annotated[str, Path()],
                 dev: CurrentDevice):
    run_status = {"succeeded": "finished", "failed": "failed",
                  "canceled": "stopped"}[body.status]
    async with db.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "UPDATE job SET status = %s, error = %s, result = %s::jsonb,"
                "  finished_at = now() WHERE id = %s AND device_id = %s"
                " RETURNING id", (body.status, body.error, json.dumps(body.result),
                                  job_id, dev.id))
            if await cur.fetchone() is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    "job não é desta máquina")
            # duração = tempo de EXECUÇÃO, não a espera na fila: job.started_at
            # é setado no 1º heartbeat (início real); training_run.started_at
            # nasce no enfileiramento. Usa o do job, caindo no do run se ausente.
            await conn.execute(
                "UPDATE training_run r SET status = %s, finished_at = now(),"
                "  duration_s = EXTRACT(EPOCH FROM (now() - COALESCE("
                "     (SELECT started_at FROM job WHERE id = %s), r.started_at)))::int"
                " WHERE r.job_id = %s", (run_status, job_id, job_id))
            await conn.execute(
                "UPDATE device SET status = 'idle' WHERE id = %s"
                "  AND status = 'busy'", (dev.id,))
    return {"job_id": job_id, "status": body.status}
