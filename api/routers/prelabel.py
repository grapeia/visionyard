"""Pré-tagueamento em lote: rodar um modelo sobre as imagens e devolver caixas
que o humano corrige.

Enfileira um job `prelabel` (executado por uma máquina com capacidade
`prelabel`). As caixas voltam como anotação `origin='prelabel'`, atribuídas a
quem disparou — que as vê como rascunho e corrige; corrigir vira `correction`,
e é essa taxa que mede o quanto o humano mudou do que o modelo sugeriu.

Regra de ouro: pré-tag NUNCA sobrescreve trabalho humano. Só grava onde não há
anotação daquele autor, ou onde a que existe é ela mesma um prelabel anterior.
"""
from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from .. import auth, db
from ..audit import audit
from ..auth import Principal
from ..deps import require_role

router = APIRouter(tags=["prelabel"])


class PrelabelIn(BaseModel):
    model_slug: str = Field(min_length=1)
    version: int | None = None          # versão específica…
    alias: str | None = None            # …ou o apelido (champion por padrão)
    # escopo (exatamente um): dataset inteiro, uma imagem só (tecla w) ou um
    # lote específico de imagens (ex.: só a página aberta na rotulagem).
    dataset_slug: str | None = None
    sample_id: str | None = None
    sample_ids: list[str] | None = None
    conf: float = Field(default=0.25, ge=0.01, le=0.99)
    only_missing: bool = True           # pula quem já tem anotação minha


async def _resolver_versao(project_id: str, body: PrelabelIn) -> dict:
    m = await db.fetch_one(
        "SELECT id FROM model WHERE project_id = %s AND slug = %s",
        (project_id, body.model_slug))
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "modelo não encontrado")
    if body.version is not None:
        mv = await db.fetch_one(
            "SELECT id, version, weights_uri, schema_id FROM model_version"
            " WHERE model_id = %s AND version = %s", (m["id"], body.version))
    else:
        alias = body.alias or "champion"
        mv = await db.fetch_one(
            "SELECT mv.id, mv.version, mv.weights_uri, mv.schema_id"
            "  FROM model_alias a JOIN model_version mv ON mv.id = a.model_version_id"
            " WHERE a.model_id = %s AND a.alias = %s", (m["id"], alias))
        if mv is None:      # sem apelido: cai para a última versão
            mv = await db.fetch_one(
                "SELECT id, version, weights_uri, schema_id FROM model_version"
                " WHERE model_id = %s ORDER BY version DESC LIMIT 1", (m["id"],))
    if mv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "nenhuma versão do modelo para usar")
    return mv


@router.post("/projects/{project_slug}/prelabel",
             status_code=status.HTTP_201_CREATED)
async def enfileirar_prelabel(
        body: PrelabelIn, project_slug: Annotated[str, Path()],
        request: Request,
        actor: Annotated[Principal, Depends(require_role("engineer"))]):
    ids = list(dict.fromkeys(body.sample_ids or []))   # dedup, preserva ordem
    if sum([bool(body.dataset_slug), bool(body.sample_id), bool(ids)]) != 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "informe exatamente um escopo: dataset_slug, sample_id OU sample_ids")
    proj = await db.fetch_one("SELECT id FROM project WHERE slug = %s",
                              (project_slug,))
    if proj is None:      # superuser passa por require_role sem checar existência
        raise HTTPException(status.HTTP_404_NOT_FOUND, "projeto não encontrado")
    if body.dataset_slug:
        alvo = await db.fetch_one(
            "SELECT id FROM dataset WHERE project_id = %s AND slug = %s"
            "   AND archived_at IS NULL", (proj["id"], body.dataset_slug))
        erro = "dataset não encontrado"
    elif body.sample_id:
        alvo = await db.fetch_one(
            "SELECT id FROM sample WHERE project_id = %s AND id = %s",
            (proj["id"], body.sample_id))
        erro = "imagem não encontrada"
    else:
        achados = await db.fetch_one(
            "SELECT count(*) AS n FROM sample"
            " WHERE project_id = %s AND id = ANY(%s::uuid[])", (proj["id"], ids))
        alvo = achados if achados["n"] == len(ids) else None
        erro = "alguma imagem não pertence a este projeto"
    if alvo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, erro)
    mv = await _resolver_versao(proj["id"], body)

    payload = {"model_version_id": str(mv["id"]),
               "weights_uri": mv["weights_uri"],
               "schema_id": str(mv["schema_id"]),
               "dataset_slug": body.dataset_slug,
               "sample_id": body.sample_id,
               "sample_ids": ids or None,
               "conf": body.conf, "only_missing": body.only_missing,
               "project_slug": project_slug}
    async with db.connection() as conn:
        async with conn.transaction():
            job = await db.fetch_one(
                "INSERT INTO job (project_id, type, payload, requires,"
                " created_by)"
                " VALUES (%s,'prelabel',%s::jsonb,%s::jsonb,%s) RETURNING id",
                (proj["id"], json.dumps(payload),
                 json.dumps({"prelabel": True}), actor.id), conn=conn)
            await audit(actor, "prelabel.enqueue", "job", str(job["id"]),
                        project_id=str(proj["id"]),
                        after={"modelo": body.model_slug,
                               "versao": mv["version"],
                               "dataset": body.dataset_slug,
                               "conf": body.conf},
                        ip=auth.client_ip(request), conn=conn)
    return {"job_id": str(job["id"]), "model_version": mv["version"],
            "status": "queued"}


@router.post("/projects/{project_slug}/datasets/{dataset_slug}/prelabel/discard")
async def descartar_prelabels(
        project_slug: Annotated[str, Path()],
        dataset_slug: Annotated[str, Path()], request: Request,
        actor: Annotated[Principal, Depends(require_role("engineer"))]):
    """Remove os pré-tags rascunho das imagens deste dataset — de todos os
    autores. Só apaga saída de máquina não tocada (`origin='prelabel'` e
    `status='draft'`): anotação humana e pré-tag já corrigido (`correction`) ou
    revisado ficam intactos. Reverte as imagens para 'sem anotação'. Sem volta.
    """
    ds = await db.fetch_one(
        "SELECT d.id, p.id AS project_id FROM dataset d"
        "  JOIN project p ON p.id = d.project_id"
        " WHERE p.slug = %s AND d.slug = %s AND d.archived_at IS NULL",
        (project_slug, dataset_slug))
    if ds is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dataset não encontrado")
    # DELETE só de rascunhos de prelabel: nenhum está em versão congelada (o
    # freeze só pega reviewed/submitted), então não há FK de dataset_version_item
    # apontando para eles. annotation_review cascateia; annotation_history
    # sobrevive (perdeu a FK na migração 009) — o log de auditoria fica.
    async with db.connection() as conn:
        async with conn.transaction():
            rows = await db.fetch_all(
                "DELETE FROM annotation a"
                " WHERE a.origin = 'prelabel' AND a.status = 'draft'"
                "   AND a.sample_id IN (SELECT ss.sample_id FROM sample_source ss"
                "     JOIN dataset_source dsrc"
                "       ON dsrc.data_source_id = ss.data_source_id"
                "    WHERE dsrc.dataset_id = %s) RETURNING a.id",
                (ds["id"],), conn=conn)
            n = len(rows)
            await audit(actor, "prelabel.discard", "dataset", str(ds["id"]),
                        project_id=str(ds["project_id"]),
                        after={"removidos": n},
                        ip=auth.client_ip(request), conn=conn)
    return {"removidos": n}
