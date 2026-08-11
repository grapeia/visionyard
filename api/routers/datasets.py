"""Aba Dataset: definição, origens, prévia e congelamento de versões."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path as P, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator

from .. import auth, db
from ..config import settings
from ..datasets import (assign_splits, build_manifest, resolve_items,
                        schemas_compativeis, write_manifest)
from ..deps import audit, require_role
from .projects import SLUG_RE

router = APIRouter(tags=["datasets"])


class Roi(BaseModel):
    """Região de interesse: caixa normalizada [x,y,w,h] em frações 0–1.

    `x,y,w,h` é a caixa canônica que o pipeline recorta (treino, avaliação e
    inferência aplicam a MESMA). `anchor`/`w_pct`/`h_pct` são só dica da UI para
    repopular os presets — o engine ignora e usa a caixa.
    """
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)
    anchor: str | None = Field(default=None, max_length=20)
    w_pct: float | None = Field(default=None, ge=0.0, le=1.0)
    h_pct: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _dentro(self) -> "Roi":
        # Ultrapassar a borda cortaria fora da imagem: o recorte em pixels ficaria
        # sem sentido e o des-recorte na inferência escalaria errado.
        if self.x + self.w > 1.0 + 1e-6 or self.y + self.h > 1.0 + 1e-6:
            raise ValueError("ROI ultrapassa a borda da imagem (x+w ou y+h > 1)")
        return self


class DatasetIn(BaseModel):
    slug: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["train", "eval", "benchmark"] = "train"
    roi: Roi | None = None

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not SLUG_RE.match(v):
            raise ValueError("slug deve ser minúsculo, com hífens")
        return v


class RoiIn(BaseModel):
    """Corpo do PATCH de ROI. `roi: null` remove o recorte do dataset."""
    roi: Roi | None = None


class FreezeIn(BaseModel):
    descricao: str = Field(min_length=1, max_length=500)
    split_method: Literal["random", "by_group"] = "random"
    val_ratio: float = Field(default=0.2, ge=0.05, le=0.5)
    seed: int = 42
    group_by: str = "metadata.lote"
    only_annotated: bool = True


async def _dataset(project_slug: str, dataset_slug: str) -> dict:
    row = await db.fetch_one(
        "SELECT d.id, d.slug, d.name, d.kind, d.roi, p.id AS project_id,"
        "       p.slug AS proj"
        "  FROM dataset d JOIN project p ON p.id = d.project_id"
        " WHERE p.slug = %s AND d.slug = %s AND d.archived_at IS NULL",
        (project_slug, dataset_slug))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dataset não encontrado")
    return row


@router.get("/projects/{project_slug}/datasets",
            dependencies=[Depends(require_role("viewer"))])
async def list_datasets(project_slug: Annotated[str, P()]):
    return await db.fetch_all(
        "SELECT d.slug, d.name, d.kind, d.roi, d.created_at,"
        "  (SELECT count(*) FROM dataset_source ds WHERE ds.dataset_id = d.id)"
        "     AS origens,"
        "  (SELECT count(*) FROM dataset_version v WHERE v.dataset_id = d.id)"
        "     AS versoes,"
        "  (SELECT max(version) FROM dataset_version v WHERE v.dataset_id = d.id)"
        "     AS ultima_versao"
        "  FROM dataset d JOIN project p ON p.id = d.project_id"
        " WHERE p.slug = %s AND d.archived_at IS NULL ORDER BY d.name",
        (project_slug,))


@router.post("/projects/{project_slug}/datasets",
             status_code=status.HTTP_201_CREATED)
async def create_dataset(body: DatasetIn, project_slug: Annotated[str, P()],
                         request: Request,
                         actor: Annotated[object, Depends(require_role("engineer"))]):
    proj = await db.fetch_one("SELECT id FROM project WHERE slug = %s",
                              (project_slug,))
    dup = await db.fetch_one(
        "SELECT id FROM dataset WHERE project_id = %s AND slug = %s",
        (proj["id"], body.slug))
    if dup:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"já existe dataset '{body.slug}'")
    roi_json = json.dumps(body.roi.model_dump()) if body.roi else None
    row = await db.fetch_one(
        "INSERT INTO dataset (project_id, slug, name, kind, roi, created_by)"
        " VALUES (%s,%s,%s,%s,%s::jsonb,%s)"
        " RETURNING id, slug, name, kind, roi, created_at",
        (proj["id"], body.slug, body.name, body.kind, roi_json, actor.id))
    await audit(actor, "dataset.create", "dataset", str(row["id"]),
                project_id=str(proj["id"]), after={"slug": body.slug},
                ip=auth.client_ip(request))
    return row


@router.patch("/projects/{project_slug}/datasets/{dataset_slug}")
async def update_roi(body: RoiIn, project_slug: Annotated[str, P()],
                     dataset_slug: Annotated[str, P()], request: Request,
                     actor: Annotated[object, Depends(require_role("engineer"))]):
    """Define ou remove a ROI do dataset.

    Só afeta as PRÓXIMAS versões congeladas — as já existentes carregam a ROI
    com que foram congeladas (imutável) e seguem reproduzíveis. Para um dataset
    já congelado passar a treinar com ROI, mude aqui e congele uma nova versão.
    """
    ds = await _dataset(project_slug, dataset_slug)
    roi_json = json.dumps(body.roi.model_dump()) if body.roi else None
    await db.execute("UPDATE dataset SET roi = %s::jsonb WHERE id = %s",
                     (roi_json, ds["id"]))
    await audit(actor, "dataset.roi", "dataset", str(ds["id"]),
                project_id=str(ds["project_id"]),
                before={"roi": ds["roi"]},
                after={"roi": body.roi.model_dump() if body.roi else None},
                ip=auth.client_ip(request))
    return {"slug": ds["slug"], "roi": body.roi.model_dump() if body.roi else None}


@router.get("/projects/{project_slug}/datasets/{dataset_slug}",
            dependencies=[Depends(require_role("viewer"))])
async def get_dataset(project_slug: Annotated[str, P()],
                      dataset_slug: Annotated[str, P()]):
    ds = await _dataset(project_slug, dataset_slug)
    origens = await db.fetch_all(
        "SELECT s.id, s.kind, s.name, s.uri, ds.added_at,"
        "  (SELECT count(*) FROM sample_source ss"
        "    WHERE ss.data_source_id = s.id) AS imagens"
        "  FROM dataset_source ds JOIN data_source s ON s.id = ds.data_source_id"
        " WHERE ds.dataset_id = %s ORDER BY ds.added_at", (ds["id"],))
    versoes = await db.fetch_all(
        "SELECT v.version, v.notes AS descricao, v.stats, v.split_policy, v.roi,"
        "       v.manifest_uri, v.frozen_at, u.email AS criado_por"
        "  FROM dataset_version v LEFT JOIN app_user u ON u.id = v.created_by"
        " WHERE v.dataset_id = %s ORDER BY v.version DESC", (ds["id"],))
    # Pré-tags rascunho ainda por revisar: é o que o botão "Descartar pré-tags"
    # remove. Só conta saída de máquina intocada (corrigir vira 'correction').
    pretags = await db.fetch_one(
        "SELECT count(*) AS n FROM annotation a"
        " WHERE a.origin = 'prelabel' AND a.status = 'draft'"
        "   AND a.sample_id IN (SELECT ss.sample_id FROM sample_source ss"
        "     JOIN dataset_source dsrc ON dsrc.data_source_id = ss.data_source_id"
        "    WHERE dsrc.dataset_id = %s)", (ds["id"],))
    return {**ds, "origens": origens, "versoes": versoes,
            "pretags_rascunho": pretags["n"]}


@router.put("/projects/{project_slug}/datasets/{dataset_slug}/origens/{source_id}",
            status_code=status.HTTP_204_NO_CONTENT)
async def add_origin(project_slug: Annotated[str, P()],
                     dataset_slug: Annotated[str, P()], source_id: str,
                     request: Request,
                     actor: Annotated[object, Depends(require_role("engineer"))]):
    ds = await _dataset(project_slug, dataset_slug)
    src = await db.fetch_one(
        "SELECT id FROM data_source WHERE id = %s AND project_id = %s",
        (source_id, ds["project_id"]))
    if src is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "origem não encontrada")
    await db.execute(
        "INSERT INTO dataset_source (dataset_id, data_source_id, added_by)"
        " VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
        (ds["id"], source_id, actor.id))
    await audit(actor, "dataset.origin.add", "dataset", str(ds["id"]),
                project_id=str(ds["project_id"]), after={"source": source_id},
                ip=auth.client_ip(request))


@router.delete("/projects/{project_slug}/datasets/{dataset_slug}/origens/{source_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def remove_origin(project_slug: Annotated[str, P()],
                        dataset_slug: Annotated[str, P()], source_id: str,
                        request: Request,
                        actor: Annotated[object, Depends(require_role("engineer"))]):
    """Remove a origem da definição.

    Não apaga sample nem anotação, e não toca nas versões já congeladas: elas
    continuam treináveis e reproduzíveis. Só as próximas versões deixarão de
    incluir essas imagens.
    """
    ds = await _dataset(project_slug, dataset_slug)
    await db.execute(
        "DELETE FROM dataset_source WHERE dataset_id=%s AND data_source_id=%s",
        (ds["id"], source_id))
    await audit(actor, "dataset.origin.remove", "dataset", str(ds["id"]),
                project_id=str(ds["project_id"]), before={"source": source_id},
                ip=auth.client_ip(request))


@router.get("/projects/{project_slug}/datasets/{dataset_slug}/preview",
            dependencies=[Depends(require_role("viewer"))])
async def preview(project_slug: Annotated[str, P()],
                  dataset_slug: Annotated[str, P()],
                  only_annotated: bool = True):
    """O que entraria numa versão congelada agora — sem gravar nada."""
    ds = await _dataset(project_slug, dataset_slug)
    compativeis = await schemas_compativeis(ds["project_id"])
    todos = await resolve_items(ds["id"], only_annotated=False,
                                schema_ids=compativeis)
    anotados = [r for r in todos if r["annotation_id"] is not None]
    itens = anotados if only_annotated else todos
    ultima = await db.fetch_one(
        "SELECT version, content_hash FROM dataset_version"
        " WHERE dataset_id = %s ORDER BY version DESC LIMIT 1", (ds["id"],))
    return {"imagens_nas_origens": len(todos),
            "anotadas": len(anotados),
            "pendentes": len(todos) - len(anotados),
            "entrariam": len(itens),
            "sem_dimensao": sum(1 for r in itens
                                if not (r["width"] and r["height"])),
            "proxima_versao": (ultima["version"] + 1) if ultima else 1}


@router.post("/projects/{project_slug}/datasets/{dataset_slug}/freeze",
             status_code=status.HTTP_201_CREATED)
async def freeze(body: FreezeIn, project_slug: Annotated[str, P()],
                 dataset_slug: Annotated[str, P()], request: Request,
                 actor: Annotated[object, Depends(require_role("engineer"))]):
    ds = await _dataset(project_slug, dataset_slug)
    schema = await db.fetch_one(
        "SELECT id, classes FROM label_schema WHERE project_id = %s"
        " ORDER BY version DESC LIMIT 1", (ds["project_id"],))
    classes = [c["name"] for c in schema["classes"]]

    compativeis = await schemas_compativeis(ds["project_id"])
    itens = await resolve_items(ds["id"], only_annotated=body.only_annotated,
                                schema_ids=compativeis)
    if not itens:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "nada a congelar — nenhuma imagem anotada nas origens do dataset")

    policy = {"method": body.split_method, "seed": body.seed,
              "val_ratio": body.val_ratio}
    if body.split_method == "by_group":
        policy["group_by"] = body.group_by
    try:
        assign_splits(itens, policy)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e

    origens = await db.fetch_all(
        "SELECT s.uri FROM dataset_source ds"
        "  JOIN data_source s ON s.id = ds.data_source_id"
        " WHERE ds.dataset_id = %s ORDER BY s.uri", (ds["id"],))
    ultima = await db.fetch_one(
        "SELECT version FROM dataset_version WHERE dataset_id = %s"
        " ORDER BY version DESC LIMIT 1", (ds["id"],))
    version = (ultima["version"] + 1) if ultima else 1

    items_jsonl, meta = build_manifest(
        itens, classes, [o["uri"] for o in origens], version, body.descricao,
        policy, project_slug, dataset_slug, roi=ds["roi"])
    items_hash = hashlib.sha256(items_jsonl.encode()).digest()

    # Identidade da versão = items + ROI. Com a MESMA lista de imagens, ROIs
    # diferentes materializam recortes diferentes e treinam modelos diferentes,
    # então a ROI entra no content_hash (que tem unique (dataset_id,
    # content_hash)) — senão congelar de novo só para trocar a ROI colidiria com
    # a versão antiga. `items_hash` puro segue sendo a integridade do
    # items.jsonl (vai no meta.json e no retorno).
    roi_json = json.dumps(ds["roi"]) if ds["roi"] else None
    content_hash = hashlib.sha256(
        items_jsonl.encode() + b"\x00roi:"
        + (json.dumps(ds["roi"], sort_keys=True) if ds["roi"] else "").encode()
    ).digest()

    raiz = Path(settings.manifest_root)
    # Congelar é: pegar o lock do dataset, gravar o banco, e só então publicar
    # o manifesto no lugar definitivo.
    #
    # Antes o manifesto era escrito ANTES do INSERT e o número da versão era
    # lido sem lock. Dois congelamentos simultâneos calculavam a mesma versão,
    # escreviam no mesmo diretório (o segundo sobrescrevia o primeiro) e só um
    # INSERT vencia — deixando no banco um `content_hash` que não correspondia
    # ao arquivo em disco. O split treinado não era o split registrado, e nada
    # detectava, porque ninguém confere o hash na leitura.
    async with db.connection() as conn:
        async with conn.transaction():
            # Serializa por dataset; solta sozinho no fim da transação.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                               (str(ds["id"]),))

            # Sob o lock, reler: outro congelamento pode ter entrado entre a
            # leitura lá em cima e agora.
            cur = await conn.execute(
                "SELECT version, content_hash FROM dataset_version"
                " WHERE dataset_id = %s ORDER BY version DESC LIMIT 1",
                (ds["id"],))
            atual = await cur.fetchone()
            version = (atual["version"] + 1) if atual else 1
            meta["version"] = version

            cur = await conn.execute(
                "SELECT version FROM dataset_version"
                " WHERE dataset_id = %s AND content_hash = %s",
                (ds["id"], content_hash))
            igual = await cur.fetchone()
            if igual:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"conteúdo idêntico à v{igual['version']} (mesma ROI) — "
                    f"nada mudou desde ela")

            destino = raiz / project_slug / dataset_slug / f"v{version}"
            cur = await conn.execute(
                "INSERT INTO dataset_version (dataset_id, version, schema_id,"
                " manifest_uri, content_hash, split_policy, stats, roi, notes,"
                " created_by)"
                " VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s)"
                " RETURNING id",
                (ds["id"], version, schema["id"], str(destino), content_hash,
                 json.dumps(policy), json.dumps(meta["stats"]), roi_json,
                 body.descricao, actor.id))
            dv = await cur.fetchone()
            # Índice consultável: permite diff e contagem sem abrir o manifesto.
            for it in itens:
                await conn.execute(
                    "INSERT INTO dataset_version_item (dataset_version_id,"
                    " sample_id, annotation_id, split) VALUES (%s,%s,%s,%s)",
                    (dv["id"], it["sample_id"], it["annotation_id"],
                     it["split"]))

            # Ainda dentro do lock: ninguém mais escreve neste diretório.
            out, _ = write_manifest(raiz, project_slug, dataset_slug, version,
                                    items_jsonl, meta)

    await audit(actor, "dataset.freeze", "dataset_version", str(dv["id"]),
                project_id=str(ds["project_id"]),
                after={"version": version, "stats": meta["stats"]},
                ip=auth.client_ip(request))
    return {"version": version, "manifest_uri": str(out),
            "items_sha256": items_hash.hex(), "stats": meta["stats"],
            "split_policy": policy}


@router.get("/projects/{project_slug}/datasets/{dataset_slug}/versions/{version}/diff",
            dependencies=[Depends(require_role("viewer"))])
async def diff(project_slug: Annotated[str, P()],
               dataset_slug: Annotated[str, P()], version: int,
               contra: int | None = None):
    """O que entrou e o que saiu entre duas versões.

    Responde pelo índice (`dataset_version_item`), sem baixar manifesto.
    """
    ds = await _dataset(project_slug, dataset_slug)
    base = contra if contra is not None else version - 1
    if base < 1:
        return {"de": None, "para": version, "entraram": [], "sairam": [],
                "nota": "primeira versão — tudo é novo"}
    rows = await db.fetch_all(
        """
        WITH a AS (SELECT i.sample_id FROM dataset_version_item i
                     JOIN dataset_version v ON v.id = i.dataset_version_id
                    WHERE v.dataset_id = %s AND v.version = %s),
             b AS (SELECT i.sample_id FROM dataset_version_item i
                     JOIN dataset_version v ON v.id = i.dataset_version_id
                    WHERE v.dataset_id = %s AND v.version = %s)
        SELECT 'entrou' AS mov, s.uri FROM b JOIN sample s ON s.id = b.sample_id
         WHERE b.sample_id NOT IN (SELECT sample_id FROM a)
        UNION ALL
        SELECT 'saiu', s.uri FROM a JOIN sample s ON s.id = a.sample_id
         WHERE a.sample_id NOT IN (SELECT sample_id FROM b)
        """, (ds["id"], base, ds["id"], version))
    return {"de": base, "para": version,
            "entraram": [r["uri"] for r in rows if r["mov"] == "entrou"],
            "sairam": [r["uri"] for r in rows if r["mov"] == "saiu"]}
