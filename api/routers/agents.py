"""Porta HTTP do agente: autenticação por `X-Agent-Token` e forma do corpo.

A regra de cada passo (quem pode reclamar o quê, o que uma escrita pode tocar)
mora em `services/agents.py`. Aqui ficam só a rota, o modelo de entrada e a
tradução do retorno — inclusive as chaves em português que o agente já consome.

Autenticação é por device, nunca pela porta de usuário: `CurrentDevice` resolve
o header próprio e todas as rotas deste arquivo passam por ele.
"""
from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Path
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..deps import CurrentDevice
from ..services import agents as svc

router = APIRouter(tags=["agents"], prefix="/agent")


# --------------------------------------------------------------------------- #
# Presença
# --------------------------------------------------------------------------- #

class RegisterIn(BaseModel):
    capabilities: dict = Field(default_factory=dict)
    agent_version: str | None = None


@router.post("/register")
async def register(body: RegisterIn, dev: CurrentDevice):
    await svc.registrar_presenca(dev.id, body.capabilities)
    return {"device_id": dev.id, "slug": dev.slug}


class HeartbeatIn(BaseModel):
    status: Literal["idle", "busy"] = "idle"


@router.post("/heartbeat")
async def heartbeat(body: HeartbeatIn, dev: CurrentDevice):
    await svc.bater_ponto(dev.id, body.status)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Claim
# --------------------------------------------------------------------------- #

@router.post("/claim")
async def claim(dev: CurrentDevice):
    return {"job": await svc.reclamar_job(dev.id)}


# --------------------------------------------------------------------------- #
# Manifesto (só quem é dono do job de treino lê)
# --------------------------------------------------------------------------- #

@router.get("/jobs/{job_id}/manifest")
async def manifest_meta(job_id: Annotated[str, Path()], dev: CurrentDevice):
    """meta.json: classes, split e estatísticas. Pequeno — vai como JSON."""
    d = await svc.diretorio_manifesto(job_id, dev.id)
    return json.loads(svc.arquivo_do_manifesto(d, "meta.json").read_text())


@router.get("/jobs/{job_id}/manifest/items")
async def manifest_items(job_id: Annotated[str, Path()], dev: CurrentDevice):
    """items.jsonl: uma linha por imagem (uri, split, boxes). Pode passar de 100
    MB — vai como arquivo, o agente lê em streaming."""
    d = await svc.diretorio_manifesto(job_id, dev.id)
    return FileResponse(svc.arquivo_do_manifesto(d, "items.jsonl"),
                        media_type="application/x-ndjson")


# --------------------------------------------------------------------------- #
# Progresso
# --------------------------------------------------------------------------- #

class JobHeartbeatIn(BaseModel):
    progress: dict = Field(default_factory=dict)   # {"epoch": 12, "de": 100}


@router.post("/jobs/{job_id}/heartbeat")
async def job_heartbeat(body: JobHeartbeatIn, job_id: Annotated[str, Path()],
                        dev: CurrentDevice):
    return await svc.atualizar_progresso(job_id, dev.id, body.progress)


class Evento(BaseModel):
    seq: int = Field(ge=1)
    level: Literal["debug", "info", "warn", "error"] = "info"
    message: str


class EventosIn(BaseModel):
    eventos: list[Evento]


@router.post("/jobs/{job_id}/events")
async def push_eventos(body: EventosIn, job_id: Annotated[str, Path()],
                       dev: CurrentDevice):
    gravados = await svc.gravar_eventos(
        job_id, dev.id, [e.model_dump() for e in body.eventos])
    return {"gravados": gravados}


class Metrica(BaseModel):
    epoch: int = Field(ge=0)
    key: str = Field(min_length=1, max_length=64)
    value: float


class MetricasIn(BaseModel):
    metricas: list[Metrica]


@router.post("/jobs/{job_id}/metrics")
async def push_metricas(body: MetricasIn, job_id: Annotated[str, Path()],
                        dev: CurrentDevice):
    gravadas = await svc.gravar_metricas(
        job_id, dev.id, [m.model_dump() for m in body.metricas])
    return {"gravadas": gravadas}


# --------------------------------------------------------------------------- #
# Pré-tagueamento: alvos e escrita das predições
# --------------------------------------------------------------------------- #

@router.get("/jobs/{job_id}/targets")
async def prelabel_targets(job_id: Annotated[str, Path()], dev: CurrentDevice):
    return await svc.alvos_prelabel(job_id, dev.id)


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
    gravadas = await svc.gravar_prelabels(
        job_id, dev.id, [p.model_dump() for p in body.predicoes])
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
    return await svc.registrar_modelo(
        job_id, dev.id, weights_uri=body.weights_uri, sha256=body.sha256,
        tamanho=body.bytes, metrics=body.metrics)


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
    return await svc.registrar_build(
        job_id, dev.id, formato=body.format, uri=body.uri, sha256=body.sha256,
        tamanho=body.bytes, target=body.target, bench=body.bench)


# --------------------------------------------------------------------------- #
# Avaliação (medida contra uma versão de benchmark)
# --------------------------------------------------------------------------- #

class EvaluationIn(BaseModel):
    metrics: dict = Field(default_factory=dict)
    artifacts_uri: str | None = None


@router.post("/jobs/{job_id}/evaluation")
async def registrar_avaliacao(body: EvaluationIn, job_id: Annotated[str, Path()],
                              dev: CurrentDevice):
    return await svc.registrar_avaliacao(
        job_id, dev.id, metrics=body.metrics, artifacts_uri=body.artifacts_uri)


# --------------------------------------------------------------------------- #
# Deployment: o device puxa o build e ativa
# --------------------------------------------------------------------------- #

@router.get("/deployments/pending")
async def deployments_pendentes(dev: CurrentDevice):
    rows = await svc.deployments_pendentes(dev.id)
    return {"deployments": [dict(r) for r in rows]}


class DeployState(BaseModel):
    state: Literal["active", "failed"]
    error: str | None = None


@router.post("/deployments/{dep_id}/state")
async def deployment_estado(body: DeployState, dep_id: Annotated[str, Path()],
                            dev: CurrentDevice):
    await svc.atualizar_deployment(dep_id, dev.id, estado=body.state,
                                   error=body.error)
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
    await svc.finalizar_job(job_id, dev.id, status_final=body.status,
                            error=body.error, result=body.result)
    return {"job_id": job_id, "status": body.status}
