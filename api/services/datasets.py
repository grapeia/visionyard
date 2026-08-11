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
"""
from __future__ import annotations

import hashlib
import json
import random
from datetime import UTC, datetime
from pathlib import Path

from .. import db

MANIFEST_VERSION = "1.0"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def schemas_compativeis(project_id: str) -> list:
    """Ids dos schemas cujas anotações ainda valem sob o schema atual.

    Acrescentar classe no fim mantém os índices antigos válidos; renomear,
    remover ou reordenar não. Caminha do schema atual para trás enquanto cada
    passo declarar `back_compatible`, e para no primeiro que quebrou.

    Sem isto, `cls=1` feito sob o schema v1 entraria como `cls=1` do v3 — outra
    classe, sem erro nenhum, e o modelo aprenderia o rótulo trocado.
    """
    linha = await db.fetch_all(
        "SELECT id, version, back_compatible FROM label_schema"
        " WHERE project_id = %s ORDER BY version DESC", (project_id,))
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


async def resolve_items(dataset_id: str, only_annotated: bool = True,
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
        """, {"ds": dataset_id, "schemas": schema_ids})
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
    """
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
