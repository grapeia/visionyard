"""Aba Modelos: o registro. Versões imutáveis de um modelo e os apelidos que
apontam para elas.

O conceito vem do Model Registry do MLflow: a versão é o artefato congelado
(pesos + métricas), e o *alias* é um ponteiro móvel — `champion` é o que está
valendo, `challenger` é o candidato em avaliação. Promover é mover o ponteiro,
não tocar na versão; assim dá para voltar atrás sem reconstruir nada.
"""
from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, Field, model_validator

from .. import auth, db
from ..deps import audit, require_role

router = APIRouter(tags=["models"])

# Apelidos com significado fixo: champion vale em produção, challenger é o
# candidato, baseline é a referência a bater. Restringir evita apelido solto que
# ninguém sabe o que significa depois.
ALIASES = {"champion", "challenger", "baseline"}


class AliasIn(BaseModel):
    version: int = Field(ge=1)


async def _model(project_slug: str, model_slug: str) -> dict:
    row = await db.fetch_one(
        "SELECT m.id, m.slug, m.name, m.task_type, m.created_at, p.id AS project_id"
        "  FROM model m JOIN project p ON p.id = m.project_id"
        " WHERE p.slug = %s AND m.slug = %s AND m.archived_at IS NULL",
        (project_slug, model_slug))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "modelo não encontrado")
    return row


@router.get("/projects/{project_slug}/models",
            dependencies=[Depends(require_role("viewer"))])
async def list_models(project_slug: Annotated[str, Path()]):
    return await db.fetch_all(
        "SELECT m.slug, m.name, m.task_type, m.created_at,"
        "  (SELECT count(*) FROM model_version v WHERE v.model_id = m.id) AS versoes,"
        "  (SELECT max(version) FROM model_version v WHERE v.model_id = m.id) AS ultima,"
        "  (SELECT jsonb_object_agg(a.alias, mv.version)"
        "     FROM model_alias a JOIN model_version mv ON mv.id = a.model_version_id"
        "    WHERE a.model_id = m.id) AS aliases"
        "  FROM model m JOIN project p ON p.id = m.project_id"
        " WHERE p.slug = %s AND m.archived_at IS NULL ORDER BY m.name",
        (project_slug,))


@router.get("/projects/{project_slug}/models/{model_slug}",
            dependencies=[Depends(require_role("viewer"))])
async def get_model(project_slug: Annotated[str, Path()],
                    model_slug: Annotated[str, Path()]):
    m = await _model(project_slug, model_slug)
    versoes = await db.fetch_all(
        "SELECT mv.id, mv.version, mv.metrics, mv.weights_uri, mv.bytes, mv.status,"
        "       mv.created_at, mv.run_id, e.slug AS experimento,"
        "       d.slug AS dataset, dv.version AS dataset_version"
        "  FROM model_version mv"
        "  LEFT JOIN training_run r ON r.id = mv.run_id"
        "  LEFT JOIN experiment e ON e.id = r.experiment_id"
        "  LEFT JOIN dataset_version dv ON dv.id = r.dataset_version_id"
        "  LEFT JOIN dataset d ON d.id = dv.dataset_id"
        " WHERE mv.model_id = %s ORDER BY mv.version DESC", (m["id"],))
    # builds (onnx/engine) por versão — o artefato implantável de cada uma
    builds = await db.fetch_all(
        "SELECT mv.version, mb.format, mb.target, mb.uri, mb.bytes, mb.bench,"
        "       mb.created_at, dev.slug AS built_on"
        "  FROM model_build mb JOIN model_version mv ON mv.id = mb.model_version_id"
        "  LEFT JOIN device dev ON dev.id = mb.built_on_device"
        " WHERE mv.model_id = %s ORDER BY mb.created_at DESC", (m["id"],))
    por_versao: dict = {}
    for b in builds:
        por_versao.setdefault(b["version"], []).append(b)
    for v in versoes:
        v["builds"] = por_versao.get(v["version"], [])
    aliases = await db.fetch_all(
        "SELECT a.alias, mv.version, a.moved_at, u.display_name AS por"
        "  FROM model_alias a JOIN model_version mv ON mv.id = a.model_version_id"
        "  LEFT JOIN app_user u ON u.id = a.moved_by"
        " WHERE a.model_id = %s ORDER BY a.alias", (m["id"],))
    # avaliações: a mais recente por (versão, benchmark, ARTEFATO). O artefato é
    # o formato do build (pt quando não há build), então pt/onnx/engine/DLA
    # aparecem como linhas distintas — é o que permite a comparação.
    avals = await db.fetch_all(
        "SELECT DISTINCT ON (mv.version, d.slug, dv.version,"
        "                    COALESCE(mb.format,'pt'), ev.model_build_id)"
        "       mv.version, d.slug AS benchmark, dv.version AS benchmark_version,"
        "       COALESCE(mb.format,'pt') AS format, mb.target, ev.metrics,"
        "       ev.created_at"
        "  FROM evaluation ev JOIN model_version mv ON mv.id = ev.model_version_id"
        "  JOIN dataset_version dv ON dv.id = ev.dataset_version_id"
        "  JOIN dataset d ON d.id = dv.dataset_id"
        "  LEFT JOIN model_build mb ON mb.id = ev.model_build_id"
        " WHERE mv.model_id = %s"
        " ORDER BY mv.version, d.slug, dv.version, COALESCE(mb.format,'pt'),"
        "          ev.model_build_id, ev.created_at DESC", (m["id"],))
    for v in versoes:
        v["avaliacoes"] = [a for a in avals if a["version"] == v["version"]]
    return {**m, "versoes": versoes, "aliases": aliases}


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
             status_code=status.HTTP_201_CREATED)
async def exportar(body: ExportIn, project_slug: Annotated[str, Path()],
                   model_slug: Annotated[str, Path()], version: int,
                   request: Request,
                   actor: Annotated[object, Depends(require_role("engineer"))]):
    """Enfileira a compilação de uma versão para um formato implantável.

    ONNX é portátil; o engine TensorRT é específico do dispositivo (JetPack/TRT),
    por isso a compilação roda NA máquina-alvo (capacidade `infer`) e o build
    guarda em qual device e com que alvo foi gerado."""
    m = await _model(project_slug, model_slug)
    mv = await db.fetch_one(
        "SELECT id, weights_uri FROM model_version"
        " WHERE model_id = %s AND version = %s", (m["id"], version))
    if mv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"versão v{version} não existe")

    payload = {"model_version_id": str(mv["id"]), "weights_uri": mv["weights_uri"],
               "format": body.format, "imgsz": body.imgsz,
               "precision": body.precision, "dla": body.dla,
               "dynamic": body.dynamic,
               "model_slug": model_slug, "version": version,
               "project_slug": project_slug}
    job = await db.fetch_one(
        "INSERT INTO job (project_id, type, payload, requires, created_by)"
        " VALUES (%s,'export',%s::jsonb,%s::jsonb,%s) RETURNING id",
        (m["project_id"], json.dumps(payload), json.dumps({"infer": True}),
         actor.id))
    await audit(actor, "model.export", "model_version", str(mv["id"]),
                project_id=str(m["project_id"]),
                after={"format": body.format, "precision": body.precision,
                       "imgsz": body.imgsz, "dynamic": body.dynamic},
                ip=auth.client_ip(request))
    return {"job_id": str(job["id"]), "version": version, "status": "queued"}


class EvaluateIn(BaseModel):
    dataset_slug: str
    dataset_version: int = Field(ge=1)
    imgsz: int = Field(default=640, ge=64, le=2048)
    # QUAL artefato medir: 'pt' (o peso) ou um build compilado. precision/dla
    # escolhem a variante do engine — é assim que se compara int8 vs fp16 vs DLA.
    format: Literal["pt", "onnx", "engine"] = "pt"
    precision: str | None = None
    dla: bool | None = None


async def _artefato_para_avaliar(model_id, version, weights_uri, body):
    """Resolve o artefato a medir → (uri, model_build_id, rótulo). 'pt' usa o
    peso; os demais resolvem o build compilado, com a variante certa."""
    if body.format == "pt":
        return weights_uri, None, "pt"
    build = await db.fetch_one(
        "SELECT mb.id, mb.uri, mb.target FROM model_build mb"
        "  JOIN model_version mv ON mv.id = mb.model_version_id"
        " WHERE mv.model_id = %(m)s AND mv.version = %(v)s AND mb.format = %(f)s"
        "   AND (%(p)s::text IS NULL OR mb.target->>'precision' = %(p)s)"
        "   AND (%(d)s::bool IS NULL OR (mb.target->>'dla')::bool = %(d)s)"
        " ORDER BY mb.created_at DESC LIMIT 1",
        {"m": model_id, "v": version, "f": body.format,
         "p": body.precision, "d": body.dla})
    if build is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"não há build {body.format} da v{version} para medir — compile antes")
    t = build["target"] or {}
    rotulo = body.format + (f" {t['precision']}" if t.get("precision") else "")
    rotulo += " din" if t.get("dynamic") else ""
    rotulo += " DLA" if t.get("dla") else ""
    return build["uri"], str(build["id"]), rotulo


@router.post("/projects/{project_slug}/models/{model_slug}/versions/{version}/evaluate",
             status_code=status.HTTP_201_CREATED)
async def avaliar(body: EvaluateIn, project_slug: Annotated[str, Path()],
                  model_slug: Annotated[str, Path()], version: int,
                  request: Request,
                  actor: Annotated[object, Depends(require_role("engineer"))]):
    """Enfileira a medição de um ARTEFATO (peso ou build) contra uma versão de
    dataset (o benchmark). Além do mAP, mede pré/inferência/pós no alvo — é o
    que permite comparar pt × engine × DLA. Roda numa máquina `infer`."""
    m = await _model(project_slug, model_slug)
    mv = await db.fetch_one(
        "SELECT id, weights_uri, schema_id FROM model_version"
        " WHERE model_id = %s AND version = %s", (m["id"], version))
    if mv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"versão v{version} não existe")
    dv = await db.fetch_one(
        "SELECT v.id FROM dataset_version v JOIN dataset d ON d.id = v.dataset_id"
        " WHERE d.project_id = %s AND d.slug = %s AND v.version = %s",
        (m["project_id"], body.dataset_slug, body.dataset_version))
    if dv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "versão de benchmark não encontrada — congele antes")
    uri, build_id, rotulo = await _artefato_para_avaliar(
        m["id"], version, mv["weights_uri"], body)

    payload = {"model_version_id": str(mv["id"]), "artifact_uri": uri,
               "model_build_id": build_id, "format": body.format,
               "rotulo": rotulo, "schema_id": str(mv["schema_id"]),
               "dataset_version_id": str(dv["id"]),
               "dataset_slug": body.dataset_slug, "imgsz": body.imgsz,
               "project_slug": project_slug}
    job = await db.fetch_one(
        "INSERT INTO job (project_id, type, payload, requires, created_by)"
        " VALUES (%s,'evaluate',%s::jsonb,%s::jsonb,%s) RETURNING id",
        (m["project_id"], json.dumps(payload), json.dumps({"infer": True}),
         actor.id))
    await audit(actor, "model.evaluate", "model_version", str(mv["id"]),
                project_id=str(m["project_id"]),
                after={"benchmark": body.dataset_slug, "artefato": rotulo,
                       "benchmark_version": body.dataset_version},
                ip=auth.client_ip(request))
    return {"job_id": str(job["id"]), "version": version, "artefato": rotulo,
            "status": "queued"}


class DeployIn(BaseModel):
    version: int = Field(ge=1)
    format: Literal["onnx", "engine"] = "engine"
    device_slug: str


@router.post("/projects/{project_slug}/models/{model_slug}/deploy",
             status_code=status.HTTP_201_CREATED)
async def implantar(body: DeployIn, project_slug: Annotated[str, Path()],
                    model_slug: Annotated[str, Path()], request: Request,
                    actor: Annotated[object, Depends(require_role("engineer"))]):
    """Marca um build para ser implantado num dispositivo. O agente daquele
    device puxa o artefato e ativa; a troca do modelo em produção é do coletor,
    que lê o artefato do caminho conhecido."""
    m = await _model(project_slug, model_slug)
    build = await db.fetch_one(
        "SELECT mb.id, mb.target FROM model_build mb JOIN model_version mv"
        "  ON mv.id = mb.model_version_id"
        " WHERE mv.model_id = %s AND mv.version = %s AND mb.format = %s"
        " ORDER BY mb.created_at DESC LIMIT 1",
        (m["id"], body.version, body.format))
    if build is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"não há build {body.format} da v{body.version} — compile antes")
    dev = await db.fetch_one("SELECT id, device_class FROM device WHERE slug = %s",
                             (body.device_slug,))
    if dev is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device não encontrado")
    # engine TensorRT é específico da classe do dispositivo que o compilou:
    # implantar num alvo de outra classe cria um pending que a máquina não
    # consegue carregar. ONNX é portátil, então não trava.
    alvo_classe = (build["target"] or {}).get("device_class")
    if body.format == "engine" and alvo_classe and alvo_classe != dev["device_class"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"o build engine é para '{alvo_classe}', mas a máquina é "
            f"'{dev['device_class']}' — compile para essa classe")
    dep = await db.fetch_one(
        "INSERT INTO deployment (device_id, model_build_id, state, requested_by)"
        " VALUES (%s,%s,'pending',%s) RETURNING id",
        (dev["id"], build["id"], actor.id))
    await audit(actor, "model.deploy", "deployment", str(dep["id"]),
                project_id=str(m["project_id"]),
                after={"device": body.device_slug, "versao": body.version,
                       "format": body.format}, ip=auth.client_ip(request))
    return {"deployment_id": str(dep["id"]), "device": body.device_slug,
            "state": "pending"}


@router.get("/projects/{project_slug}/deployments",
            dependencies=[Depends(require_role("viewer"))])
async def list_deployments(project_slug: Annotated[str, Path()]):
    """Implantações do projeto, com device, build e estado."""
    return await db.fetch_all(
        "SELECT dep.id, dev.slug AS device, dep.state, dep.requested_at,"
        "       dep.activated_at, dep.error, m.slug AS model_slug, mv.version,"
        "       mb.format, mb.target"
        "  FROM deployment dep"
        "  JOIN device dev ON dev.id = dep.device_id"
        "  JOIN model_build mb ON mb.id = dep.model_build_id"
        "  JOIN model_version mv ON mv.id = mb.model_version_id"
        "  JOIN model m ON m.id = mv.model_id"
        " WHERE m.project_id = (SELECT id FROM project WHERE slug = %s)"
        "   AND dep.state IN ('pending','active','failed')"
        " ORDER BY dep.requested_at DESC", (project_slug,))


@router.put("/projects/{project_slug}/models/{model_slug}/aliases/{alias}")
async def set_alias(project_slug: Annotated[str, Path()],
                    model_slug: Annotated[str, Path()],
                    alias: Annotated[str, Path()], body: AliasIn,
                    request: Request,
                    actor: Annotated[object, Depends(require_role("engineer"))]):
    """Aponta um apelido para uma versão (promover). Move o ponteiro; a versão
    não muda."""
    if alias not in ALIASES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"apelido '{alias}' inválido ({', '.join(sorted(ALIASES))})")
    m = await _model(project_slug, model_slug)
    mv = await db.fetch_one(
        "SELECT id FROM model_version WHERE model_id = %s AND version = %s",
        (m["id"], body.version))
    if mv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"versão v{body.version} não existe")
    before = await db.fetch_one(
        "SELECT mv.version FROM model_alias a"
        "  JOIN model_version mv ON mv.id = a.model_version_id"
        " WHERE a.model_id = %s AND a.alias = %s", (m["id"], alias))
    await db.execute(
        "INSERT INTO model_alias (model_id, alias, model_version_id, moved_by)"
        " VALUES (%s,%s,%s,%s)"
        " ON CONFLICT (model_id, alias) DO UPDATE"
        "   SET model_version_id = EXCLUDED.model_version_id,"
        "       moved_by = EXCLUDED.moved_by, moved_at = now()",
        (m["id"], alias, mv["id"], actor.id))
    await audit(actor, "model.alias", "model", str(m["id"]),
                project_id=str(m["project_id"]),
                before={"alias": alias, "version": before["version"] if before else None},
                after={"alias": alias, "version": body.version},
                ip=auth.client_ip(request))
    return {"alias": alias, "version": body.version}


@router.delete("/projects/{project_slug}/models/{model_slug}/aliases/{alias}",
               status_code=status.HTTP_204_NO_CONTENT)
async def remove_alias(project_slug: Annotated[str, Path()],
                       model_slug: Annotated[str, Path()],
                       alias: Annotated[str, Path()], request: Request,
                       actor: Annotated[object, Depends(require_role("engineer"))]):
    m = await _model(project_slug, model_slug)
    row = await db.fetch_one(
        "DELETE FROM model_alias WHERE model_id = %s AND alias = %s RETURNING alias",
        (m["id"], alias))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "apelido não definido")
    await audit(actor, "model.alias.remove", "model", str(m["id"]),
                project_id=str(m["project_id"]), before={"alias": alias},
                ip=auth.client_ip(request))
