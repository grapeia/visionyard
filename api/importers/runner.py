"""Execução de uma importação: lê da origem, grava no projeto, reporta.

Uma função só, igual para toda origem. O importador normaliza; aqui está a
única escrita no banco — assim uma origem nova não pode inventar um jeito
diferente de gravar, nem escapar das regras de deduplicação.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .. import db
from .base import Importer, ImportReport, SourceSample, map_classes

CHUNK = 1 << 20


def _hashes_and_size(path: Path) -> tuple[bytes, bytes, int]:
    """(md5, sha256, bytes). Calcula os dois numa passada só.

    md5 é a chave de deduplicação porque é o que o GCS fornece de graça; sha256
    vai junto porque os bytes já estão passando aqui e custa quase nada.
    """
    md5, sha = hashlib.md5(), hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            md5.update(chunk)
            sha.update(chunk)
            size += len(chunk)
    return md5.digest(), sha.digest(), size


def _dimensions(path: Path) -> tuple[int, int] | None:
    from PIL import Image
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:      # arquivo corrompido não aborta o lote
        return None


async def _resolve_author(email: str | None, fallback_id: str) -> str:
    """Anotação importada fica com o autor original se ele existir aqui.

    Sem isso, uma importação apagaria a autoria de todo mundo, atribuindo tudo
    a quem clicou em importar.
    """
    if not email:
        return fallback_id
    row = await db.fetch_one(
        "SELECT id FROM app_user WHERE lower(email) = %s", (email.lower(),))
    return str(row["id"]) if row else fallback_id


async def run_import(*, project_id: str, schema_id: str,
                     schema_classes: list[str], importer: Importer,
                     actor_id: str, class_overrides: dict[str, str] | None,
                     source_id: str | None, dry_run: bool,
                     report: ImportReport,
                     progress_cb=None) -> ImportReport:
    mapping, unmapped = map_classes(importer.classes(), schema_classes,
                                    class_overrides)
    for name in unmapped:
        report.unmapped_classes.setdefault(name, 0)
        report.warn(f"classe '{name}' não existe no schema do projeto — "
                    f"as caixas dela não serão importadas")

    seen = 0
    # Digests já vistos NESTA passada. No modo real quem garante a unicidade é
    # o banco; no dry-run não há gravação, então sem isto a mesma imagem
    # repetida na origem seria contada como duas criações — e o relatório
    # prometeria mais do que a importação entregaria.
    seen_digests: set[bytes] = set()
    for sample in importer.read(report):
        seen += 1
        if progress_cb and seen % 25 == 0:
            await progress_cb(seen)
        await _import_one(sample, project_id=project_id, schema_id=schema_id,
                          mapping=mapping, actor_id=actor_id,
                          source_id=source_id, dry_run=dry_run, report=report,
                          seen_digests=seen_digests)
    if progress_cb:
        await progress_cb(seen)
    return report


async def _import_one(sample: SourceSample, *, project_id: str, schema_id: str,
                      mapping: dict[str, int], actor_id: str,
                      source_id: str | None, dry_run: bool,
                      report: ImportReport,
                      seen_digests: set[bytes] | None = None) -> None:
    sha256 = None
    width, height = sample.width, sample.height
    size = sample.bytes_size

    if sample.md5 is not None:
        # Origem já forneceu a identidade (caso do GCS): nada a baixar.
        digest = sample.md5
    elif sample.local_path:
        path = Path(sample.local_path)
        if not path.is_file():
            report.skip(sample.key, "arquivo sumiu durante a importação")
            return
        digest, sha256, size = _hashes_and_size(path)
        if not (width and height):
            dims = _dimensions(path)
            if dims is None:
                report.skip(sample.key,
                            "não consegui ler as dimensões da imagem")
                return
            width, height = dims
    else:
        report.skip(sample.key, "origem não forneceu md5 nem arquivo local")
        return

    # Caixas: mapeia nomes; o que não mapeia é contado, não descartado calado.
    boxes = []
    for box in sample.boxes:
        if box.cls_name not in mapping:
            report.unmapped_classes[box.cls_name] = (
                report.unmapped_classes.get(box.cls_name, 0) + 1)
            continue
        boxes.append({"cls": mapping[box.cls_name],
                      "cx": round(box.cx, 6), "cy": round(box.cy, 6),
                      "w": round(box.w, 6), "h": round(box.h, 6)})

    # `[]` significa "anotado, sem objetos" — um exemplo negativo. Se a origem
    # TINHA caixas e todas caíram fora do schema, gravar `[]` ensinaria o
    # modelo que ali não há nada, que é o oposto da verdade. Melhor não
    # importar a anotação e dizer por quê.
    todas_descartadas = bool(sample.boxes) and not boxes
    if todas_descartadas:
        report.skip(sample.key,
                    "todas as caixas são de classes fora do schema — "
                    "anotação não importada (use class_overrides)")

    existing = await db.fetch_one(
        "SELECT id FROM sample WHERE project_id = %s AND md5 = %s",
        (project_id, digest))

    if dry_run:
        duplicada = digest in seen_digests if seen_digests is not None else False
        if existing or duplicada:
            report.samples_existing += 1
        else:
            report.samples_created += 1
        if seen_digests is not None:
            seen_digests.add(digest)
        if sample.annotated:
            report.annotations_created += 1
        return

    author_id = await _resolve_author(sample.author_email, actor_id)

    async with db.connection() as conn:
        async with conn.transaction():
            if existing:
                sample_id = existing["id"]
                report.samples_existing += 1
            else:
                cur = await conn.execute(
                    "INSERT INTO sample (project_id, source_id, md5, sha256,"
                    " uri, width, height, bytes, metadata, imported_by)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)"
                    # O predicado repete o do índice parcial criado na
                    # migration 008 — sem ele o Postgres não casa a inferência.
                    " ON CONFLICT (project_id, md5) WHERE md5 IS NOT NULL"
                    " DO NOTHING"
                    " RETURNING id",
                    (project_id, source_id, digest, sha256, sample.uri,
                     width, height, size, json.dumps(sample.metadata),
                     actor_id))
                row = await cur.fetchone()
                if row is None:
                    # corrida com outra importação do mesmo arquivo
                    cur = await conn.execute(
                        "SELECT id FROM sample WHERE project_id=%s AND md5=%s",
                        (project_id, digest))
                    row = await cur.fetchone()
                    report.samples_existing += 1
                else:
                    report.samples_created += 1
                sample_id = row["id"]

            # Pertencimento é a várias origens: sem isto, a mesma foto vinda
            # de um segundo prefixo continuava presa à origem original, e um
            # dataset que referenciasse só a nova origem resolvia zero imagens.
            if source_id:
                await conn.execute(
                    "INSERT INTO sample_source (sample_id, data_source_id)"
                    " VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (sample_id, source_id))

            if not sample.annotated or todas_descartadas:
                return

            # Reimportar atualiza a anotação daquele autor em vez de duplicar,
            # MAS nunca sobrescreve uma já revisada: alguém conferiu e corrigiu
            # aquelas caixas à mão. Antes, reimportar o dump devolvia as caixas
            # originais mantendo o status `reviewed` — e como a resolução
            # prioriza justamente o revisado, o dado pior vencia o melhor e o
            # congelamento seguinte o eternizava.
            cur = await conn.execute(
                "INSERT INTO annotation (sample_id, schema_id, boxes, origin,"
                " status, author_id, updated_by)"
                " VALUES (%s,%s,%s::jsonb,'import','submitted',%s,%s)"
                " ON CONFLICT (sample_id, author_id) DO UPDATE"
                "    SET boxes = EXCLUDED.boxes, updated_at = now(),"
                "        updated_by = EXCLUDED.updated_by"
                "  WHERE annotation.status <> 'reviewed'"
                " RETURNING (xmax = 0) AS inserted, id",
                (sample_id, schema_id, json.dumps(boxes), author_id, actor_id))
            ann = await cur.fetchone()
            if ann is None:
                # o WHERE do DO UPDATE barrou: a anotação existente é revisada
                report.skip(sample.key,
                            "anotação já revisada — mantida, importação "
                            "não sobrescreve trabalho conferido")
                return
            if ann["inserted"]:
                report.annotations_created += 1
            else:
                report.annotations_updated += 1
            # `boxes_before` para o histórico dizer o que se perdeu, não só o
            # que ficou.
            await conn.execute(
                "INSERT INTO annotation_history (annotation_id, sample_id,"
                " actor_id, action, boxes_before, boxes_after)"
                " SELECT %s,%s,%s,%s,"
                "        (SELECT boxes FROM annotation WHERE id = %s),"
                "        %s::jsonb",
                (ann["id"], sample_id, actor_id,
                 "create" if ann["inserted"] else "update",
                 ann["id"], json.dumps(boxes)))
