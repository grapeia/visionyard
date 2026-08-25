"""Datasets: definição viva (origens) e versões congeladas (manifesto).

A separação que sustenta tudo:

  * **Dataset** é editável — a lista de origens muda quando você quiser.
  * **DatasetVersion** é imutável — congelar resolve as origens em uma lista
    concreta de imagens, sorteia o split uma única vez e grava o manifesto.

Congelar resolve as origens **por consulta ao banco**, não relistando o bucket:
as imagens entraram como `sample` na importação, cada uma ligada ao seu
`data_source`. Assim o bucket é listado uma vez, e não a cada versão.

O manifesto sai em dois arquivos, como o export do Label Studio faz (que também
separa dados e metadados, com hash de integridade):

    <raiz>/<projeto>/<dataset>/v<N>/meta.json     descrição, origens, stats
    <raiz>/<projeto>/<dataset>/v<N>/items.jsonl   uma linha por imagem

JSONL e não um JSON único porque em centenas de milhares de imagens o arquivo
passa de 100 MB: em linhas dá para ler em streaming e o diff entre versões
vira diff de texto.

**Toda função daqui que fala com o banco recebe `conn`** (item 15 do PLANO).
Sem isso nada no tipo impede uma sequência leia-decida-escreva de acontecer em
três conexões diferentes: cada `db.fetch_*` sem `conn=` pega a sua, e o
congelamento — que relê a versão sob lock consultivo justamente para decidir o
que gravar — é o lugar do repositório onde isso doeria mais.

Quem **abre** a conexão e a transação é a função pública deste módulo, não o
router: onde a transação começa e termina é decisão de domínio, e deixá-la no
router faria a mesma regra ser descrita em dois lugares.
"""
from __future__ import annotations

import hashlib
import json
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from .. import db
from ..audit import audit
from ..auth import Principal
from ..config import settings

MANIFEST_VERSION = "1.0"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def schemas_compativeis(conn: db.Conn, project_id: str) -> list:
    """Ids dos schemas cujas anotações ainda valem sob o schema atual.

    Acrescentar classe no fim mantém os índices antigos válidos; renomear,
    remover ou reordenar não. Caminha do schema atual para trás enquanto cada
    passo declarar `back_compatible`, e para no primeiro que quebrou.

    Sem isto, `cls=1` feito sob o schema v1 entraria como `cls=1` do v3 — outra
    classe, sem erro nenhum, e o modelo aprenderia o rótulo trocado.
    """
    linha = await db.fetch_all(
        "SELECT id, version, back_compatible FROM label_schema"
        " WHERE project_id = %s ORDER BY version DESC", (project_id,),
        conn=conn)
    ids = []
    for i, s in enumerate(linha):
        ids.append(str(s["id"]))
        # o campo descreve a mudança que CRIOU esta versão: se ela quebrou a
        # compatibilidade, as anteriores não valem mais
        if i > 0 and not s["back_compatible"]:
            break
        if i == 0 and not s["back_compatible"] and len(linha) > 1:
            break
    return ids


async def resolve_items(conn: db.Conn, dataset_id: str,
                        only_annotated: bool = True,
                        schema_ids: list | None = None) -> list:
    """Imagens que as origens do dataset trazem hoje, com a anotação vigente.

    Precedência entre rotuladores, explícita: revisada > enviada > o resto.
    Antes o critério era só "revisada primeiro, depois a mais recente", o que
    fazia um rascunho salvo depois vencer uma anotação enviada — e pior, uma
    anotação marcada `needs_review` (que significa "provavelmente errada")
    vencer uma boa, se fosse a mais nova.

    O desempate final por `a.id` existe para o congelamento ser reproduzível:
    duas anotações com o mesmo timestamp (importação em lote) escolheriam
    arbitrariamente, e dois freezes do mesmo dado gerariam manifestos
    diferentes.
    """
    rows = await db.fetch_all(
        """
        SELECT DISTINCT ON (s.id)
               s.id AS sample_id, s.uri, s.md5, s.width, s.height,
               s.metadata, a.id AS annotation_id, a.boxes, a.status
          FROM dataset_source ds
          JOIN sample_source ss ON ss.data_source_id = ds.data_source_id
          JOIN sample s ON s.id = ss.sample_id
          LEFT JOIN annotation a
                 ON a.sample_id = s.id
                AND a.status IN ('reviewed', 'submitted')
                AND (%(schemas)s::uuid[] IS NULL
                     OR a.schema_id = ANY(%(schemas)s::uuid[]))
         WHERE ds.dataset_id = %(ds)s
         ORDER BY s.id,
                  CASE a.status WHEN 'reviewed' THEN 0
                                WHEN 'submitted' THEN 1
                                ELSE 2 END,
                  a.updated_at DESC NULLS LAST,
                  a.id DESC NULLS LAST
        """, {"ds": dataset_id, "schemas": schema_ids}, conn=conn)
    if only_annotated:
        rows = [r for r in rows if r["annotation_id"] is not None]
    return rows


def assign_splits(items: list, policy: dict) -> None:
    """Grava `split` em cada item, uma vez só.

    O sorteio acontece aqui, no congelamento — e não a cada treino. É o que
    torna dois treinos comparáveis: mesma versão, mesmo split.

    `by_group` existe para quando a origem é vídeo: frames consecutivos são
    quase idênticos e, sorteados individualmente, o mesmo instante cai em
    treino e validação — o modelo "acerta" o que já viu e o mAP infla. Para
    imagens independentes de bucket, `random` é adequado.
    """
    method = policy.get("method", "random")
    seed = int(policy.get("seed", 42))
    val_ratio = float(policy.get("val_ratio", 0.2))
    rnd = random.Random(seed)

    if method == "by_group":
        key = policy.get("group_by", "metadata.lote").split(".")[-1]
        com_chave = sum(1 for it in items
                        if (it.get("metadata") or {}).get(key) is not None)
        if com_chave == 0:
            # Sem a chave, cada item viraria um grupo de um e o agrupamento
            # degradaria para aleatório — mas o manifesto continuaria
            # declarando by_group. Nenhum importador de hoje grava 'lote', o
            # que tornava esse o caso normal, não a exceção.
            raise ValueError(
                f"nenhuma imagem tem 'metadata.{key}': o agrupamento não teria "
                f"efeito e a divisão sairia aleatória. Escolha divisão "
                f"aleatória ou importe as imagens com esse campo.")
        groups: dict = {}
        for it in items:
            g = str((it.get("metadata") or {}).get(key, it["sample_id"]))
            groups.setdefault(g, []).append(it)
        names = sorted(groups)
        rnd.shuffle(names)
        # Nunca todos os grupos para validação: com um grupo só (uma
        # importação de um vídeo, por exemplo) o arredondamento levava tudo
        # para val e a versão congelava com train vazio, sem erro nenhum.
        n_val = min(len(names) - 1, max(1, round(len(names) * val_ratio))) \
            if len(names) >= 2 else 0
        val = set(names[:n_val])
        for g, group_items in groups.items():
            for it in group_items:
                it["split"] = "val" if g in val else "train"
        return

    order = sorted(items, key=lambda i: str(i["sample_id"]))
    rnd.shuffle(order)
    n_val = min(len(order) - 1, max(1, round(len(order) * val_ratio))) \
        if len(order) >= 2 else 0
    for i, it in enumerate(order):
        it["split"] = "val" if i < n_val else "train"


def build_manifest(items: list, classes: list, origins: list, version: int,
                   description: str, policy: dict, project_slug: str,
                   dataset_slug: str, roi: dict | None = None) -> tuple[str, dict]:
    """Devolve (items.jsonl, meta.json) — meta ainda sem o hash.

    `roi` (caixa normalizada {x,y,w,h} ou None) viaja no meta.json para o agente
    aplicar o MESMO recorte no treino/avaliação. As caixas do items.jsonl ficam
    em coordenadas do quadro CHEIO — o agente reprojeta ao materializar, para
    que o manifesto siga independente da ROI (mudar a ROI não reescreve caixas).

    Levanta `HTTPException` 409 se alguma imagem estiver sem md5 (ver abaixo).
    """
    # `sample.md5` é anulável desde a 008 — linha pendente de backfill, que só
    # existe em base que passou pela 006 e teve o placeholder zerado. Sem md5 o
    # agente não tem chave de cache nem nome de arquivo para materializar, então
    # o item não pode entrar no manifesto. Recusar aqui é o que transforma o
    # `TypeError` no meio da geração num 409 que diz qual imagem trava.
    #
    # A mensagem NÃO manda reimportar: o dedup de `importers/runner.py` casa por
    # md5, e o índice parcial da 008 ignora linha nula — reimportar cria uma
    # SEGUNDA imagem com o md5 verdadeiro e deixa a anotação humana presa à
    # antiga, que continua sendo o que `resolve_items` devolve. O congelamento
    # seguiria barrado, agora com a duplicata que a própria 008 avisa. Como
    # `sample` é imutável por trigger, também não há backfill por UPDATE: sair
    # daqui é retirar a origem do dataset (ver PLANO, item 11).
    sem_md5 = [it["uri"] for it in items if it.get("md5") is None]
    if sem_md5:
        amostra = ", ".join(sem_md5[:3]) + (" …" if len(sem_md5) > 3 else "")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{len(sem_md5)} imagem(ns) sem md5, resquício do backfill da "
            f"migration 008 — sem identidade de conteúdo o agente não sabe "
            f"baixar nem cachear o arquivo. Reimportar não resolve (o dedup é "
            f"por md5 e a linha nula nunca casa: nasce uma imagem nova e a "
            f"anotação fica na antiga); retire do dataset a origem dessas "
            f"imagens. Primeiras: {amostra}")

    lines = []
    per_class = dict.fromkeys(classes, 0)
    n_train = n_val = 0
    sem_dimensao = 0
    for it in items:
        boxes = [[int(b["cls"]), round(float(b["cx"]), 6), round(float(b["cy"]), 6),
                  round(float(b["w"]), 6), round(float(b["h"]), 6)]
                 for b in (it.get("boxes") or [])]
        for b in boxes:
            cls = int(b[0])
            if 0 <= cls < len(classes):
                per_class[classes[cls]] += 1
        if it["split"] == "val":
            n_val += 1
        else:
            n_train += 1
        if not (it.get("width") and it.get("height")):
            sem_dimensao += 1
        lines.append(json.dumps({
            "uri": it["uri"],
            "md5": bytes(it["md5"]).hex(),
            "split": it["split"],
            "w": it.get("width"), "h": it.get("height"),
            "boxes": boxes}, separators=(",", ":"), ensure_ascii=False))

    items_jsonl = "\n".join(lines) + ("\n" if lines else "")
    meta = {
        "manifest_version": MANIFEST_VERSION,
        "project": project_slug,
        "dataset": dataset_slug,
        "version": version,
        "descricao": description,
        "classes": classes,
        "origens": origins,
        "split_policy": policy,
        "roi": roi,
        "stats": {"imagens": len(items), "train": n_train, "val": n_val,
                  "boxes_por_classe": per_class,
                  "sem_dimensao": sem_dimensao},
        "congelado_em": _now_iso(),
        # Gravar quem produziu o arquivo: se o formato mudar ou aparecer um bug
        # na conversão, dá para saber qual versão gerou cada manifesto em vez
        # de adivinhar. (O export do Label Studio faz o mesmo.)
        "gerado_por": f"visionyard-api/{MANIFEST_VERSION}",
    }
    return items_jsonl, meta


def write_manifest(root: Path, project_slug: str, dataset_slug: str,
                   version: int, items_jsonl: str, meta: dict) -> tuple[Path, str]:
    """Grava os dois arquivos e devolve (diretório, hash do items.jsonl)."""
    items_hash = hashlib.sha256(items_jsonl.encode()).hexdigest()
    meta = {**meta, "items_sha256": items_hash,
            "items_linhas": items_jsonl.count("\n")}
    out = root / project_slug / dataset_slug / f"v{version}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "items.jsonl").write_text(items_jsonl)
    (out / "meta.json").write_text(
        json.dumps(meta, indent=1, ensure_ascii=False))
    return out, items_hash


# =========================================================================== #
# A regra que o router chamava. Cada função abaixo recebe `conn`; as públicas
# (sem `_`) abrem a conexão e, quando escrevem, a transação.
# =========================================================================== #

async def _projeto_id(conn: db.Conn, project_slug: str) -> str:
    """O id do projeto, ou 404.

    Existe aqui, e não em `deps`, porque `deps.projeto_ou_404` alcança o pool
    por conta própria — o que este módulo deixou de fazer.

    Sem filtrar `archived_at`: quem barra escrita em projeto arquivado é o
    `require_role`, com 409 e antes do handler. Repetir a regra aqui a
    descreveria em dois lugares, e o 404 daqui mentiria sobre um projeto que
    existe e é legível.
    """
    row = await db.fetch_one(
        "SELECT id FROM project WHERE slug = %s", (project_slug,), conn=conn)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "projeto não encontrado")
    return str(row["id"])


async def _dataset_ou_404(conn: db.Conn, project_slug: str,
                          dataset_slug: str) -> dict[str, Any]:
    row = await db.fetch_one(
        "SELECT d.id, d.slug, d.name, d.kind, d.roi, p.id AS project_id,"
        "       p.slug AS proj"
        "  FROM dataset d JOIN project p ON p.id = d.project_id"
        " WHERE p.slug = %s AND d.slug = %s AND d.archived_at IS NULL",
        (project_slug, dataset_slug), conn=conn)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dataset não encontrado")
    return row


async def listar(project_slug: str) -> list[dict[str, Any]]:
    async with db.connection() as conn:
        return await db.fetch_all(
            "SELECT d.slug, d.name, d.kind, d.roi, d.created_at,"
            "  (SELECT count(*) FROM dataset_source ds"
            "    WHERE ds.dataset_id = d.id) AS origens,"
            "  (SELECT count(*) FROM dataset_version v"
            "    WHERE v.dataset_id = d.id) AS versoes,"
            "  (SELECT max(version) FROM dataset_version v"
            "    WHERE v.dataset_id = d.id) AS ultima_versao"
            "  FROM dataset d JOIN project p ON p.id = d.project_id"
            " WHERE p.slug = %s AND d.archived_at IS NULL ORDER BY d.name",
            (project_slug,), conn=conn)


async def criar(project_slug: str, actor: Principal, *, slug: str, name: str,
                kind: str, roi: dict | None, ip: str | None) -> dict[str, Any]:
    async with db.connection() as conn:
        project_id = await _projeto_id(conn, project_slug)
        dup = await db.fetch_one(
            "SELECT id FROM dataset WHERE project_id = %s AND slug = %s",
            (project_id, slug), conn=conn)
        if dup:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"já existe dataset '{slug}'")
        async with conn.transaction():
            row = await db.fetch_row(
                "INSERT INTO dataset (project_id, slug, name, kind, roi,"
                " created_by) VALUES (%s,%s,%s,%s,%s::jsonb,%s)"
                " RETURNING id, slug, name, kind, roi, created_at",
                (project_id, slug, name, kind,
                 json.dumps(roi) if roi else None, actor.id), conn=conn)
            await audit(actor, "dataset.create", "dataset", str(row["id"]),
                        project_id=project_id, after={"slug": slug},
                        ip=ip, conn=conn)
    return row


async def definir_roi(project_slug: str, dataset_slug: str, actor: Principal,
                      *, roi: dict | None, ip: str | None) -> dict[str, Any]:
    """Define ou remove a ROI do dataset.

    Só afeta as PRÓXIMAS versões congeladas — as já existentes carregam a ROI
    com que foram congeladas (imutável) e seguem reproduzíveis.
    """
    async with db.connection() as conn:
        ds = await _dataset_ou_404(conn, project_slug, dataset_slug)
        async with conn.transaction():
            await db.execute(
                "UPDATE dataset SET roi = %s::jsonb WHERE id = %s",
                (json.dumps(roi) if roi else None, ds["id"]), conn=conn)
            await audit(actor, "dataset.roi", "dataset", str(ds["id"]),
                        project_id=str(ds["project_id"]),
                        before={"roi": ds["roi"]}, after={"roi": roi},
                        ip=ip, conn=conn)
    return {"slug": ds["slug"], "roi": roi}


async def detalhe(project_slug: str, dataset_slug: str) -> dict[str, Any]:
    async with db.connection() as conn:
        ds = await _dataset_ou_404(conn, project_slug, dataset_slug)
        origens = await db.fetch_all(
            "SELECT s.id, s.kind, s.name, s.uri, ds.added_at,"
            "  (SELECT count(*) FROM sample_source ss"
            "    WHERE ss.data_source_id = s.id) AS imagens"
            "  FROM dataset_source ds"
            "  JOIN data_source s ON s.id = ds.data_source_id"
            " WHERE ds.dataset_id = %s ORDER BY ds.added_at",
            (ds["id"],), conn=conn)
        versoes = await db.fetch_all(
            "SELECT v.version, v.notes AS descricao, v.stats, v.split_policy,"
            "       v.roi, v.manifest_uri, v.frozen_at, u.email AS criado_por"
            "  FROM dataset_version v"
            "  LEFT JOIN app_user u ON u.id = v.created_by"
            " WHERE v.dataset_id = %s ORDER BY v.version DESC",
            (ds["id"],), conn=conn)
        # Pré-tags rascunho ainda por revisar: é o que o botão "Descartar
        # pré-tags" remove. Só conta saída de máquina intocada (corrigir vira
        # 'correction').
        pretags = await db.fetch_row(
            "SELECT count(*) AS n FROM annotation a"
            " WHERE a.origin = 'prelabel' AND a.status = 'draft'"
            "   AND a.sample_id IN (SELECT ss.sample_id FROM sample_source ss"
            "     JOIN dataset_source dsrc"
            "       ON dsrc.data_source_id = ss.data_source_id"
            "    WHERE dsrc.dataset_id = %s)", (ds["id"],), conn=conn)
    return {**ds, "origens": origens, "versoes": versoes,
            "pretags_rascunho": pretags["n"]}


async def vincular_origem(project_slug: str, dataset_slug: str,
                          actor: Principal, *, source_id: str,
                          ip: str | None) -> None:
    async with db.connection() as conn:
        ds = await _dataset_ou_404(conn, project_slug, dataset_slug)
        src = await db.fetch_one(
            "SELECT id FROM data_source WHERE id = %s AND project_id = %s",
            (source_id, ds["project_id"]), conn=conn)
        if src is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "origem não encontrada")
        async with conn.transaction():
            await db.execute(
                "INSERT INTO dataset_source (dataset_id, data_source_id,"
                " added_by) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (ds["id"], source_id, actor.id), conn=conn)
            await audit(actor, "dataset.origin.add", "dataset", str(ds["id"]),
                        project_id=str(ds["project_id"]),
                        after={"source": source_id}, ip=ip, conn=conn)


async def desvincular_origem(project_slug: str, dataset_slug: str,
                             actor: Principal, *, source_id: str,
                             ip: str | None) -> None:
    """Remove a origem da definição.

    Não apaga sample nem anotação, e não toca nas versões já congeladas: elas
    continuam treináveis e reproduzíveis. Só as próximas versões deixarão de
    incluir essas imagens.
    """
    async with db.connection() as conn:
        ds = await _dataset_ou_404(conn, project_slug, dataset_slug)
        async with conn.transaction():
            await db.execute(
                "DELETE FROM dataset_source"
                " WHERE dataset_id=%s AND data_source_id=%s",
                (ds["id"], source_id), conn=conn)
            await audit(actor, "dataset.origin.remove", "dataset",
                        str(ds["id"]), project_id=str(ds["project_id"]),
                        before={"source": source_id}, ip=ip, conn=conn)


async def previa(project_slug: str, dataset_slug: str,
                 only_annotated: bool) -> dict[str, Any]:
    """O que entraria numa versão congelada agora — sem gravar nada."""
    async with db.connection() as conn:
        ds = await _dataset_ou_404(conn, project_slug, dataset_slug)
        compativeis = await schemas_compativeis(conn, ds["project_id"])
        todos = await resolve_items(conn, ds["id"], only_annotated=False,
                                    schema_ids=compativeis)
        ultima = await db.fetch_one(
            "SELECT version FROM dataset_version"
            " WHERE dataset_id = %s ORDER BY version DESC LIMIT 1",
            (ds["id"],), conn=conn)
    anotados = [r for r in todos if r["annotation_id"] is not None]
    itens = anotados if only_annotated else todos
    return {"imagens_nas_origens": len(todos),
            "anotadas": len(anotados),
            "pendentes": len(todos) - len(anotados),
            "entrariam": len(itens),
            "sem_dimensao": sum(1 for r in itens
                                if not (r["width"] and r["height"])),
            # Congelar recusa se houver qualquer uma (ver `build_manifest`).
            # Sai aqui para a tela não oferecer um botão que sempre dá 409.
            "sem_md5": sum(1 for r in itens if r["md5"] is None),
            "proxima_versao": (ultima["version"] + 1) if ultima else 1}


async def diff(project_slug: str, dataset_slug: str, version: int,
               contra: int | None) -> dict[str, Any]:
    """O que entrou e o que saiu entre duas versões.

    Responde pelo índice (`dataset_version_item`), sem baixar manifesto.
    """
    async with db.connection() as conn:
        ds = await _dataset_ou_404(conn, project_slug, dataset_slug)
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
            SELECT 'entrou' AS mov, s.uri
              FROM b JOIN sample s ON s.id = b.sample_id
             WHERE b.sample_id NOT IN (SELECT sample_id FROM a)
            UNION ALL
            SELECT 'saiu', s.uri FROM a JOIN sample s ON s.id = a.sample_id
             WHERE a.sample_id NOT IN (SELECT sample_id FROM b)
            """, (ds["id"], base, ds["id"], version), conn=conn)
    return {"de": base, "para": version,
            "entraram": [r["uri"] for r in rows if r["mov"] == "entrou"],
            "sairam": [r["uri"] for r in rows if r["mov"] == "saiu"]}


async def congelar(project_slug: str, dataset_slug: str, actor: Principal, *,
                   descricao: str, split_method: str, val_ratio: float,
                   seed: int, group_by: str, only_annotated: bool,
                   ip: str | None) -> dict[str, Any]:
    """Sela uma versão: resolve as origens, sorteia o split e grava o manifesto.

    A parte cara (resolver as origens, montar o items.jsonl) fica **fora** da
    transação de propósito: ela é leitura e CPU, e segurá-la sob o lock
    consultivo faria um congelamento grande bloquear todos os outros do mesmo
    dataset pelo tempo inteiro. O que vale é o que se relê sob o lock.
    """
    async with db.connection() as conn:
        ds = await _dataset_ou_404(conn, project_slug, dataset_slug)
        schema = await db.fetch_row(
            "SELECT id, classes FROM label_schema WHERE project_id = %s"
            " ORDER BY version DESC LIMIT 1", (ds["project_id"],), conn=conn)
        classes = [c["name"] for c in schema["classes"]]

        compativeis = await schemas_compativeis(conn, ds["project_id"])
        itens = await resolve_items(conn, ds["id"],
                                    only_annotated=only_annotated,
                                    schema_ids=compativeis)
        if not itens:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "nada a congelar — nenhuma imagem anotada nas origens do "
                "dataset")

        policy: dict[str, Any] = {"method": split_method, "seed": seed,
                                  "val_ratio": val_ratio}
        if split_method == "by_group":
            policy["group_by"] = group_by
        try:
            assign_splits(itens, policy)
        except ValueError as e:
            raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e

        origens = await db.fetch_all(
            "SELECT s.uri FROM dataset_source ds"
            "  JOIN data_source s ON s.id = ds.data_source_id"
            " WHERE ds.dataset_id = %s ORDER BY s.uri", (ds["id"],), conn=conn)
        # Provisória, só para o rascunho do meta.json: quem manda é a versão
        # relida sob o lock, logo abaixo.
        ultima = await db.fetch_one(
            "SELECT version FROM dataset_version WHERE dataset_id = %s"
            " ORDER BY version DESC LIMIT 1", (ds["id"],), conn=conn)
        version = (ultima["version"] + 1) if ultima else 1

        # `build_manifest` levanta a HTTPException por conta própria (é a
        # convenção do repositório): um `except ValueError` aqui pegaria também
        # o `float()` das caixas e devolveria erro interno em inglês como se
        # fosse conflito.
        items_jsonl, meta = build_manifest(
            itens, classes, [o["uri"] for o in origens], version, descricao,
            policy, project_slug, dataset_slug, roi=ds["roi"])
        items_hash = hashlib.sha256(items_jsonl.encode()).digest()

        # Identidade da versão = items + ROI. Com a MESMA lista de imagens,
        # ROIs diferentes materializam recortes diferentes e treinam modelos
        # diferentes, então a ROI entra no content_hash (que tem unique
        # (dataset_id, content_hash)) — senão congelar de novo só para trocar a
        # ROI colidiria com a versão antiga. `items_hash` puro segue sendo a
        # integridade do items.jsonl (vai no meta.json e no retorno).
        roi_json = json.dumps(ds["roi"]) if ds["roi"] else None
        content_hash = hashlib.sha256(
            items_jsonl.encode() + b"\x00roi:"
            + (json.dumps(ds["roi"], sort_keys=True)
               if ds["roi"] else "").encode()).digest()

        raiz = Path(settings.manifest_root)
        # Congelar é: pegar o lock do dataset, gravar o banco, e só então
        # publicar o manifesto no lugar definitivo.
        #
        # Antes o manifesto era escrito ANTES do INSERT e o número da versão
        # era lido sem lock. Dois congelamentos simultâneos calculavam a mesma
        # versão, escreviam no mesmo diretório (o segundo sobrescrevia o
        # primeiro) e só um INSERT vencia — deixando no banco um `content_hash`
        # que não correspondia ao arquivo em disco. O split treinado não era o
        # split registrado, e nada detectava, porque ninguém confere o hash na
        # leitura.
        async with conn.transaction():
            # Serializa por dataset; solta sozinho no fim da transação.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                               (str(ds["id"]),))

            # Sob o lock, reler: outro congelamento pode ter entrado entre a
            # leitura lá em cima e agora.
            atual = await db.fetch_one(
                "SELECT version FROM dataset_version"
                " WHERE dataset_id = %s ORDER BY version DESC LIMIT 1",
                (ds["id"],), conn=conn)
            version = (atual["version"] + 1) if atual else 1
            meta["version"] = version

            igual = await db.fetch_one(
                "SELECT version FROM dataset_version"
                " WHERE dataset_id = %s AND content_hash = %s",
                (ds["id"], content_hash), conn=conn)
            if igual:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"conteúdo idêntico à v{igual['version']} (mesma ROI) — "
                    f"nada mudou desde ela")

            destino = raiz / project_slug / dataset_slug / f"v{version}"
            dv = await db.fetch_row(
                "INSERT INTO dataset_version (dataset_id, version, schema_id,"
                " manifest_uri, content_hash, split_policy, stats, roi, notes,"
                " created_by)"
                " VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s)"
                " RETURNING id",
                (ds["id"], version, schema["id"], str(destino), content_hash,
                 json.dumps(policy), json.dumps(meta["stats"]), roi_json,
                 descricao, actor.id), conn=conn)
            # Índice consultável: permite diff e contagem sem abrir o manifesto.
            for it in itens:
                await db.execute(
                    "INSERT INTO dataset_version_item (dataset_version_id,"
                    " sample_id, annotation_id, split) VALUES (%s,%s,%s,%s)",
                    (dv["id"], it["sample_id"], it["annotation_id"],
                     it["split"]), conn=conn)

            # Ainda dentro do lock: ninguém mais escreve neste diretório.
            out, _ = write_manifest(raiz, project_slug, dataset_slug, version,
                                    items_jsonl, meta)

            await audit(actor, "dataset.freeze", "dataset_version",
                        str(dv["id"]), project_id=str(ds["project_id"]),
                        after={"version": version, "stats": meta["stats"]},
                        ip=ip, conn=conn)
    return {"version": version, "manifest_uri": str(out),
            "items_sha256": items_hash.hex(), "stats": meta["stats"],
            "split_policy": policy}
