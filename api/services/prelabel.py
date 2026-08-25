"""Pré-tagueamento em lote: rodar um modelo sobre as imagens e devolver caixas
que o humano corrige.

Enfileira um job `prelabel` (executado por uma máquina com capacidade
`prelabel`). As caixas voltam como anotação `origin='prelabel'`, atribuídas a
quem disparou — que as vê como rascunho e corrige; corrigir vira `correction`,
e é essa taxa que mede o quanto o humano mudou do que o modelo sugeriu.

Regra de ouro: pré-tag NUNCA sobrescreve trabalho humano. Quem a garante é o
`WHERE` do `DO UPDATE` em `services/agents.py: gravar_prelabels`, no momento da
escrita — não a ordem das operações daqui.

Segue a forma do item 15 do PLANO: toda função que fala com o banco recebe
`conn`, quem abre a conexão é a função pública, e `conn.transaction()` vai no
MESMO `async with` da conexão.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

from contracts import jobs

from .. import db
from ..audit import audit
from ..auth import Principal
from .projects import projeto_ou_404


async def _versao_do_modelo(conn: db.Conn, project_id: str, *, model_slug: str,
                            version: int | None,
                            alias: str | None) -> dict[str, Any]:
    m = await db.fetch_one(
        "SELECT id FROM model WHERE project_id = %s AND slug = %s",
        (project_id, model_slug), conn=conn)
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "modelo não encontrado")

    if version is not None:
        mv = await db.fetch_one(
            "SELECT id, version, weights_uri, schema_id FROM model_version"
            " WHERE model_id = %s AND version = %s", (m["id"], version),
            conn=conn)
    else:
        mv = await db.fetch_one(
            "SELECT mv.id, mv.version, mv.weights_uri, mv.schema_id"
            "  FROM model_alias a"
            "  JOIN model_version mv ON mv.id = a.model_version_id"
            " WHERE a.model_id = %s AND a.alias = %s",
            (m["id"], alias or "champion"), conn=conn)
        # Apelido pedido de propósito é exigência, não sugestão: cair calado
        # para a última versão pré-tagearia com um modelo que ninguém escolheu
        # — e o relatório ainda diria que foi o apelido pedido. O fallback vale
        # só para o 'champion' implícito, que é o padrão, não uma escolha.
        if mv is None and alias:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"apelido '{alias}' não existe neste modelo")
        if mv is None:      # sem champion: cai para a última versão
            mv = await db.fetch_one(
                "SELECT id, version, weights_uri, schema_id FROM model_version"
                " WHERE model_id = %s ORDER BY version DESC LIMIT 1",
                (m["id"],), conn=conn)
    if mv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "nenhuma versão do modelo para usar")
    return mv


async def _conferir_escopo(conn: db.Conn, project_id: str, *,
                           dataset_slug: str | None, sample_id: UUID | None,
                           ids: list[UUID]) -> None:
    """O escopo é exatamente um dos três, e tem de ser deste projeto.

    A conferência é o que impede pré-tagear imagem alheia passando o id na
    requisição: o `project_id` entra no `WHERE` de cada um dos três casos.
    """
    if sum([bool(dataset_slug), bool(sample_id), bool(ids)]) != 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "informe exatamente um escopo: dataset_slug, sample_id OU "
            "sample_ids")
    if dataset_slug:
        alvo = await db.fetch_one(
            "SELECT id FROM dataset WHERE project_id = %s AND slug = %s"
            "   AND archived_at IS NULL", (project_id, dataset_slug),
            conn=conn)
        erro = "dataset não encontrado"
    elif sample_id:
        alvo = await db.fetch_one(
            "SELECT id FROM sample WHERE project_id = %s AND id = %s",
            (project_id, sample_id), conn=conn)
        erro = "imagem não encontrada"
    else:
        achados = await db.fetch_row(
            "SELECT count(*) AS n FROM sample"
            " WHERE project_id = %s AND id = ANY(%s::uuid[])",
            (project_id, ids), conn=conn)
        alvo = achados if achados["n"] == len(ids) else None
        erro = "alguma imagem não pertence a este projeto"
    if alvo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, erro)


async def enfileirar(project_slug: str, actor: Principal, *, model_slug: str,
                     version: int | None, alias: str | None,
                     dataset_slug: str | None, sample_id: UUID | None,
                     sample_ids: list[UUID] | None, conf: float,
                     only_missing: bool, ip: str | None) -> dict[str, Any]:
    ids = list(dict.fromkeys(sample_ids or []))   # dedup, preserva ordem
    async with db.connection() as conn, conn.transaction():
        proj = await projeto_ou_404(conn, project_slug)
        await _conferir_escopo(conn, str(proj["id"]),
                               dataset_slug=dataset_slug, sample_id=sample_id,
                               ids=ids)
        mv = await _versao_do_modelo(conn, str(proj["id"]),
                                     model_slug=model_slug, version=version,
                                     alias=alias)

        # str() nos ids: o payload é JSON e quem o lê do outro lado é o agente,
        # que não tem pydantic para desserializar UUID de volta.
        payload = jobs.Prelabel(
            model_version_id=str(mv["id"]), weights_uri=mv["weights_uri"],
            schema_id=str(mv["schema_id"]), project_slug=project_slug,
            conf=conf, only_missing=only_missing, dataset_slug=dataset_slug,
            sample_id=str(sample_id) if sample_id else None,
            sample_ids=[str(i) for i in ids] or None)

        job = await db.fetch_row(
            "INSERT INTO job (project_id, type, payload, requires, created_by)"
            " VALUES (%s,'prelabel',%s::jsonb,%s::jsonb,%s) RETURNING id",
            (proj["id"], json.dumps(payload.como_dict()),
             json.dumps({"prelabel": True}), actor.id), conn=conn)
        await audit(actor, "prelabel.enqueue", "job", str(job["id"]),
                    project_id=str(proj["id"]),
                    after={"modelo": model_slug, "versao": mv["version"],
                           "dataset": dataset_slug, "conf": conf},
                    ip=ip, conn=conn)
    return {"job_id": str(job["id"]), "model_version": mv["version"],
            "status": "queued"}


async def descartar(project_slug: str, dataset_slug: str, actor: Principal,
                    ip: str | None) -> dict[str, Any]:
    """Remove os pré-tags rascunho das imagens deste dataset — de todos os
    autores. Só apaga saída de máquina não tocada (`origin='prelabel'` e
    `status='draft'`): anotação humana e pré-tag já corrigido (`correction`) ou
    revisado ficam intactos. Reverte as imagens para 'sem anotação'. Sem volta.
    """
    async with db.connection() as conn, conn.transaction():
        ds = await db.fetch_one(
            "SELECT d.id, p.id AS project_id FROM dataset d"
            "  JOIN project p ON p.id = d.project_id"
            " WHERE p.slug = %s AND d.slug = %s AND d.archived_at IS NULL",
            (project_slug, dataset_slug), conn=conn)
        if ds is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "dataset não encontrado")
        # DELETE só de rascunhos de prelabel: nenhum está em versão congelada
        # (o freeze só pega reviewed/submitted), então não há FK de
        # dataset_version_item apontando para eles. annotation_review
        # cascateia; annotation_history sobrevive (perdeu a FK na migração
        # 009) — o log de auditoria fica.
        rows = await db.fetch_all(
            "DELETE FROM annotation a"
            " WHERE a.origin = 'prelabel' AND a.status = 'draft'"
            "   AND a.sample_id IN (SELECT ss.sample_id FROM sample_source ss"
            "     JOIN dataset_source dsrc"
            "       ON dsrc.data_source_id = ss.data_source_id"
            "    WHERE dsrc.dataset_id = %s) RETURNING a.id",
            (ds["id"],), conn=conn)
        await audit(actor, "prelabel.discard", "dataset", str(ds["id"]),
                    project_id=str(ds["project_id"]),
                    after={"removidos": len(rows)}, ip=ip, conn=conn)
    return {"removidos": len(rows)}
