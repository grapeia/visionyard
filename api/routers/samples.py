"""Aba Dados: imagens do projeto e a anotação de cada uma.

As imagens são buscadas pelo navegador **direto do GCS**, por URL assinada — a
API emite a URL e não trafega bytes. Medido: a galeria sai de 28 s para 1,7 s
por página. O gargalo nunca foi o tamanho da imagem, era passar por um
intermediário.

Anotação é por (imagem, autor). Salvar mexe só na sua: o trabalho de outro
rotulador não é tocado, e é isso que permite medir concordância depois.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from .. import db
from ..auth import Principal
from ..deps import CurrentUser, projeto_ou_404, require_role

router = APIRouter(tags=["samples"])

POR_PAGINA = 60
MINIATURA_MIN = 15   # validade da URL assinada, em minutos


class Caixa(BaseModel):
    cls: int = Field(ge=0)
    cx: float
    cy: float
    w: float = Field(gt=0)
    h: float = Field(gt=0)

    @field_validator("cx", "cy")
    @classmethod
    def _dentro(cls, v: float) -> float:
        # Caixa com centro fora da imagem é sempre erro de conversão ou de
        # arrasto — deixar entrar contamina o manifesto, que é imutável.
        if not 0.0 <= v <= 1.0:
            raise ValueError("centro da caixa fora da imagem")
        return v


class AnotacaoIn(BaseModel):
    # Lista vazia é significativa: quer dizer "olhei e não há nada aqui" —
    # um exemplo negativo, diferente de "ainda não olhei".
    boxes: list[Caixa]
    status: Literal["submitted", "draft"] = "submitted"


def _assinar(uris: list) -> dict:
    """URL assinada para cada gs://. Sem credencial que assine, devolve vazio
    e a tela mostra o lugar da imagem sem quebrar."""
    from google.cloud import storage
    try:
        cliente = storage.Client()
    except Exception:
        from .buckets import _client
        try:
            cliente = _client()
        except Exception:
            return {}
    if not hasattr(getattr(cliente, "_credentials", None), "signer_email"):
        return {}
    out = {}
    for uri in uris:
        if not uri.startswith("gs://"):
            continue
        bucket, _, chave = uri[5:].partition("/")
        try:
            out[uri] = cliente.bucket(bucket).blob(chave).generate_signed_url(
                version="v4", expiration=timedelta(minutes=MINIATURA_MIN),
                method="GET")
        except Exception:
            continue
    return out


@router.get("/projects/{project_slug}/samples",
            dependencies=[Depends(require_role("viewer"))])
async def listar(project_slug: Annotated[str, Path()], user: CurrentUser,
                 pagina: int = Query(0, ge=0),
                 filtro: Literal["todas", "pendentes", "anotadas"] = "todas",
                 dataset: str | None = None):
    """Página de imagens, com URL assinada para o navegador buscar direto.

    `dataset` é escopo, não posse: a anotação vive no sample, não no dataset,
    porque a mesma imagem pode estar em vários. O escopo só restringe QUAIS
    imagens você vê — as das origens daquele dataset — para focar o esforço de
    rotulagem. A caixa que você desenha aqui vale para todo dataset que inclua
    a imagem.
    """
    proj = await projeto_ou_404(project_slug)
    # `minha` distingue "anotada por alguém" de "anotada por mim": o rotulador
    # precisa achar o que falta PARA ELE, não o que falta no geral.
    cond = {"todas": "", "pendentes": " AND a.id IS NULL",
            "anotadas": " AND a.id IS NOT NULL"}[filtro]

    # As imagens do dataset = as das origens que ele referencia.
    escopo = ""
    if dataset:
        escopo = ("""AND EXISTS (
            SELECT 1 FROM sample_source ss
              JOIN dataset_source dsc ON dsc.data_source_id = ss.data_source_id
              JOIN dataset d ON d.id = dsc.dataset_id
             WHERE ss.sample_id = s.id AND d.slug = %(ds)s
               AND d.project_id = %(p)s)""")

    params = {"p": proj["id"], "u": user.id, "ds": dataset,
              "lim": POR_PAGINA, "off": pagina * POR_PAGINA}
    sql = f"""
        SELECT s.id, s.uri, s.width, s.height, s.metadata,
               a.id AS annotation_id, a.status, a.origin,
               jsonb_array_length(COALESCE(a.boxes,'[]'::jsonb)) AS n_caixas
          FROM sample s
          LEFT JOIN annotation a ON a.sample_id = s.id AND a.author_id = %(u)s
         WHERE s.project_id = %(p)s
           {escopo}
           {cond}
         ORDER BY s.imported_at, s.id
         LIMIT %(lim)s OFFSET %(off)s"""
    linhas = await db.fetch_all(sql, params)
    # O total respeita escopo E filtro, senão a contagem no topo não bate com a
    # grade — e o filtro precisa do mesmo LEFT JOIN, porque `{cond}` fala de `a`.
    total = await db.fetch_row(
        f"""SELECT count(*) AS n FROM sample s
              LEFT JOIN annotation a ON a.sample_id = s.id AND a.author_id = %(u)s
             WHERE s.project_id = %(p)s {escopo} {cond}""", params)
    assinadas = await run_in_threadpool(_assinar, [r["uri"] for r in linhas])
    return {"pagina": pagina, "por_pagina": POR_PAGINA, "total": total["n"],
            "assina": bool(assinadas),
            "itens": [{**r, "url": assinadas.get(r["uri"])} for r in linhas]}


@router.get("/projects/{project_slug}/samples/{sample_id}",
            dependencies=[Depends(require_role("viewer"))])
async def detalhe(project_slug: Annotated[str, Path()],
                  sample_id: Annotated[UUID, Path()], user: CurrentUser):
    s = await db.fetch_one(
        "SELECT s.id, s.uri, s.width, s.height, s.metadata"
        "  FROM sample s JOIN project p ON p.id = s.project_id"
        " WHERE p.slug = %s AND s.id = %s", (project_slug, sample_id))
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "imagem não encontrada")
    minha = await db.fetch_one(
        "SELECT id, boxes, status, origin, updated_at FROM annotation"
        " WHERE sample_id = %s AND author_id = %s", (sample_id, user.id))
    # As dos outros aparecem só como contagem: ver a caixa alheia antes de
    # desenhar a sua enviesa a anotação e destrói a medida de concordância.
    outros = await db.fetch_row(
        "SELECT count(*) AS n FROM annotation"
        " WHERE sample_id = %s AND author_id <> %s", (sample_id, user.id))
    url = (await run_in_threadpool(_assinar, [s["uri"]])).get(s["uri"])
    return {**s, "url": url, "minha_anotacao": minha,
            "outros_anotaram": outros["n"]}


@router.put("/projects/{project_slug}/samples/{sample_id}/annotation")
async def salvar(body: AnotacaoIn, project_slug: Annotated[str, Path()],
                 sample_id: Annotated[UUID, Path()], request: Request,
                 user: Annotated[Principal, Depends(require_role("annotator"))]):
    """Grava a SUA anotação. A de outro rotulador não é tocada."""
    ctx = await db.fetch_one(
        "SELECT s.id, p.id AS project_id,"
        "  (SELECT id FROM label_schema WHERE project_id = p.id"
        "    ORDER BY version DESC LIMIT 1) AS schema_id,"
        "  (SELECT classes FROM label_schema WHERE project_id = p.id"
        "    ORDER BY version DESC LIMIT 1) AS classes"
        "  FROM sample s JOIN project p ON p.id = s.project_id"
        " WHERE p.slug = %s AND s.id = %s", (project_slug, sample_id))
    if ctx is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "imagem não encontrada")

    n_classes = len(ctx["classes"])
    fora = [b.cls for b in body.boxes if b.cls >= n_classes]
    if fora:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"classe {fora[0]} não existe neste projeto ({n_classes} classes)")

    caixas = [b.model_dump() for b in body.boxes]
    async with db.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT id, boxes, origin FROM annotation"
                " WHERE sample_id = %s AND author_id = %s",
                (sample_id, user.id))
            antes = await cur.fetchone()
            # Corrigir uma pré-rotulagem é 'correction', não 'manual': a
            # diferença é o que permite medir depois quanto o humano mudou do
            # que o modelo sugeriu. Se essa taxa cair muito, o ciclo virou
            # autoconfirmação — o modelo treinando na própria saída.
            # 'correction' está na tupla porque a procedência é do trabalho,
            # não do último clique: sem ela o segundo save da mesma imagem a
            # rebaixava para 'manual' e a taxa zerava justo onde o humano mais
            # mexeu.
            origem = ("correction"
                      if antes and antes["origin"] in ("prelabel", "import",
                                                       "correction")
                      else "manual")
            nova = await db.fetch_row(
                "INSERT INTO annotation (sample_id, schema_id, boxes, origin,"
                " status, author_id, updated_by)"
                " VALUES (%s,%s,%s::jsonb,%s,%s,%s,%s)"
                " ON CONFLICT (sample_id, author_id) DO UPDATE"
                "    SET boxes = EXCLUDED.boxes, status = EXCLUDED.status,"
                "        origin = EXCLUDED.origin, updated_at = now(),"
                "        updated_by = EXCLUDED.updated_by"
                " RETURNING id, status",
                (sample_id, ctx["schema_id"], json.dumps(caixas), origem,
                 body.status, user.id, user.id), conn=conn)
            await conn.execute(
                "INSERT INTO annotation_history (annotation_id, sample_id,"
                " actor_id, action, boxes_before, boxes_after)"
                " VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb)",
                (nova["id"], sample_id, user.id,
                 "update" if antes else "create",
                 json.dumps(antes["boxes"]) if antes else None,
                 json.dumps(caixas)))
    return {"id": str(nova["id"]), "status": nova["status"],
            "origin": origem, "caixas": len(caixas)}
