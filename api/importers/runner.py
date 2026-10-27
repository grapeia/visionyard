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
from ..services import parameters
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


async def _resolve_author(conn: db.Conn, email: str | None,
                          fallback_id: str) -> str:
    """Anotação importada fica com o autor original se ele existir aqui.

    Sem isso, uma importação apagaria a autoria de todo mundo, atribuindo tudo
    a quem clicou em importar.
    """
    if not email:
        return fallback_id
    row = await db.fetch_one(
        "SELECT id FROM app_user WHERE lower(email) = %s", (email.lower(),),
        conn=conn)
    return str(row["id"]) if row else fallback_id


def _uri_permitida(uri: str, buckets: list[str]) -> bool:
    """A URI gravada em `sample` é a que a grade assina com a conta de serviço,
    para qualquer viewer do projeto. Deixar passar `gs://` de um bucket fora da
    allowlist faria a API ler por essa conta o que a allowlist não libera; e a
    URI vem de dado de quem importa, seja da tarefa do LS, seja da base que ele
    escolhe. Esquema que não é `gs://` também não entra: ninguém o assinaria, e
    a tela mostraria um endereço de outro sistema. Caminho relativo, sem base,
    segue entrando como sempre entrou."""
    if "://" not in uri:
        return True
    if not uri.startswith("gs://"):
        return False
    return uri[5:].partition("/")[0] in buckets


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
                    f"as formas dela não serão importadas")

    async with db.connection() as conn:
        projeto = await db.fetch_row("SELECT task_type FROM project WHERE id = %s",
                                     (project_id,), conn=conn)
        buckets = await parameters.valor("buckets", conn)
    tarefa = projeto["task_type"]
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
        if not _uri_permitida(sample.uri, buckets):
            report.skip(sample.key, "imagem fora dos buckets permitidos")
            continue
        await _import_one(sample, project_id=project_id, schema_id=schema_id,
                          mapping=mapping, actor_id=actor_id,
                          source_id=source_id, dry_run=dry_run, report=report,
                          seen_digests=seen_digests, task_type=tarefa)
    if progress_cb:
        await progress_cb(seen)
    return report


async def _import_one(sample: SourceSample, *, project_id: str, schema_id: str,
                      mapping: dict[str, int], actor_id: str,
                      source_id: str | None, dry_run: bool,
                      report: ImportReport,
                      seen_digests: set[bytes] | None = None,
                      task_type: str = "detect") -> None:
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
        try:
            digest, sha256, size = _hashes_and_size(path)
        except OSError:
            report.skip(sample.key, "não consegui ler o arquivo da imagem")
            return
        dims = _dimensions(path)
        if dims is None:
            report.skip(sample.key, "não consegui ler as dimensões da imagem")
            return
        if ((width is not None and width != dims[0])
                or (height is not None and height != dims[1])):
            report.skip(sample.key, "dimensões da anotação divergem da imagem")
            return
        width, height = dims
    else:
        report.skip(sample.key, "origem não forneceu md5 nem arquivo local")
        return

    # Caixas: mapeia nomes; o que não mapeia é contado, não descartado calado.
    boxes: list[dict] = []
    for box in sample.boxes:
        if box.cls_name not in mapping:
            report.unmapped_classes[box.cls_name] = (
                report.unmapped_classes.get(box.cls_name, 0) + 1)
            continue
        boxes.append({"cls": mapping[box.cls_name],
                      "cx": box.cx, "cy": box.cy,
                      "w": box.w, "h": box.h})

    for poly in sample.polygons:
        if poly.cls_name not in mapping:
            report.unmapped_classes[poly.cls_name] = (
                report.unmapped_classes.get(poly.cls_name, 0) + 1)
            continue
        if task_type != "segment":
            report.skip(sample.key, "polígono exige projeto de segmentação")
            sample.annotation_incomplete = True
            continue
        boxes.append({"cls": mapping[poly.cls_name], "pts": poly.pts})
    if task_type == "segment":
        for i, forma in enumerate(boxes):
            if "pts" in forma:
                continue
            x, y = forma["cx"] - forma["w"] / 2, forma["cy"] - forma["h"] / 2
            r, b = x + forma["w"], y + forma["h"]
            boxes[i] = {"cls": forma["cls"], "pts": [[x, y], [r, y], [r, b], [x, b]]}

    # Uma forma perdida não pode transformar a imagem em negativo de treino.
    todas_descartadas = bool(sample.boxes or sample.polygons) and not boxes
    if todas_descartadas:
        report.skip(sample.key, "nenhuma forma compatível com o schema — "
                    "anotação não importada (confira class_overrides e tarefa)")
    incompleta = todas_descartadas or sample.annotation_incomplete
    if sample.annotation_incomplete:
        report.skip(sample.key, "anotação incompleta — não importada; "
                    "corrija as formas recusadas e tente novamente")

    if dry_run:
        # Sem gravação nenhuma: uma conexão só, para a contagem do relatório.
        async with db.connection() as conn:
            existing = await db.fetch_one(
                "SELECT id FROM sample WHERE project_id = %s AND md5 = %s",
                (project_id, digest), conn=conn)
        duplicada = digest in seen_digests if seen_digests is not None else False
        if existing or duplicada:
            report.samples_existing += 1
        else:
            report.samples_created += 1
        if seen_digests is not None:
            seen_digests.add(digest)
        if sample.annotated and not incompleta:
            report.annotations_created += 1
        return

    # Ler, decidir e gravar na MESMA conexão e na mesma transação. A leitura
    # do `existing` decide entre reusar a imagem e inseri-la: feita noutra
    # conexão, ela enxerga um instante diferente do que a escrita vê, e o
    # `conn.transaction()` solto — longe do `db.connection()` — abria um
    # SAVEPOINT em vez de transação sempre que a conexão já vinha lida.
    async with db.connection() as conn, conn.transaction():
        existing = await db.fetch_one(
            "SELECT id FROM sample WHERE project_id = %s AND md5 = %s",
            (project_id, digest), conn=conn)
        author_id = await _resolve_author(conn, sample.author_email, actor_id)
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
                row = await db.fetch_one(
                    "SELECT id FROM sample WHERE project_id=%s AND md5=%s",
                    (project_id, digest), conn=conn)
                if row is None:
                    # `DO NOTHING` também desiste diante de linha ainda NÃO
                    # comitada pela outra importação — que o SELECT não
                    # enxerga. Pular é o certo: quem está inserindo vai
                    # gravar a imagem, e forçar um INSERT aqui só bateria no
                    # índice e derrubaria o lote inteiro.
                    report.skip(sample.key,
                                "outra importação está gravando esta imagem")
                    return
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

        if not sample.annotated or incompleta:
            return

        # Reimportar atualiza a anotação daquele autor em vez de duplicar,
        # mas só a que a própria importação escreveu e ninguém revisou. O
        # autor da importada é quem importa, e se ele já desenhou ou corrigiu
        # aquela imagem à mão (`manual`, `correction`), trocar as caixas
        # dele pelas do arquivo apagaria trabalho humano em silêncio. Revisada
        # também fica: antes, reimportar o dump devolvia as caixas originais
        # mantendo o status `reviewed`, e como a resolução prioriza justamente
        # o revisado, o dado pior vencia o melhor e o congelamento seguinte o
        # eternizava.
        cur = await conn.execute(
            "INSERT INTO annotation (sample_id, schema_id, boxes, origin,"
            " status, author_id, updated_by)"
            " VALUES (%s,%s,%s::jsonb,'import','submitted',%s,%s)"
            " ON CONFLICT (sample_id, author_id) DO UPDATE"
            "    SET boxes = EXCLUDED.boxes, updated_at = now(),"
            "        updated_by = EXCLUDED.updated_by"
            "  WHERE annotation.origin = 'import'"
            "    AND annotation.status <> 'reviewed'"
            " RETURNING (xmax = 0) AS inserted, id",
            (sample_id, schema_id, json.dumps(boxes), author_id, actor_id))
        ann = await cur.fetchone()
        if ann is None:
            # o WHERE do DO UPDATE barrou: feita à mão, corrigida ou revisada
            report.skip(sample.key,
                        "anotação feita à mão ou revisada — mantida, "
                        "importação não sobrescreve trabalho humano")
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
