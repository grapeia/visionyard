"""Importador de Label Studio (dump sqlite).

Lê o `label_studio.sqlite3` direto, sem subir o serviço — é o caminho para
aposentar o LS sem depender dele estar de pé.

Três detalhes do formato que só aparecem olhando um dump real:

1. **Bbox em porcentagem, a partir do canto superior esquerdo.** O LS grava
   `x, y, width, height` em 0..100; YOLO quer `cx, cy, w, h` normalizado pelo
   centro. A conversão é trivial e silenciosa quando errada — desloca tudo pela
   metade da caixa, e o treino ainda "funciona", só que pior.

2. **A chave de `task.data` pode não ser `image`.** Quando o storage foi
   sincronizado sem casar com a `value=$image` do template, a chave vira
   `"$undefined$"`. Então procuramos o primeiro valor que pareça um caminho de
   imagem, em vez de exigir uma chave fixa.

3. **O caminho vem embrulhado**: `/data/local-files/?d=frames/foo.jpg`. O que
   interessa é o `d=`.
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Iterator

from .base import ImportReport, SourceBox, SourceSample

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
LABEL_RE = re.compile(r'<Label\s+value="([^"]+)"', re.IGNORECASE)


def _unwrap_path(raw: str) -> str | None:
    """`/data/local-files/?d=frames/foo.jpg` → `frames/foo.jpg`."""
    if not isinstance(raw, str):
        return None
    if "?" in raw:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        if "d" in qs and qs["d"]:
            return qs["d"][0].lstrip("/")
    if raw.startswith(("gs://", "s3://", "http://", "https://")):
        return raw
    return raw.lstrip("/") or None


def _task_path(data_json: str) -> str | None:
    """Primeiro valor de `task.data` que pareça imagem — a chave varia."""
    try:
        data = json.loads(data_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    for value in data.values():
        path = _unwrap_path(value) if isinstance(value, str) else None
        if path and Path(path.split("?")[0]).suffix.lower() in IMG_EXTS:
            return path
    return None


def _to_yolo(value: dict) -> tuple[float, float, float, float]:
    """LS (x, y, w, h em % do canto sup. esq.) → YOLO (cx, cy, w, h em 0..1)."""
    x, y = float(value["x"]), float(value["y"])
    w, h = float(value["width"]), float(value["height"])
    return ((x + w / 2) / 100.0, (y + h / 2) / 100.0, w / 100.0, h / 100.0)


class LabelStudioImporter:
    kind = "label_studio_sqlite"

    def __init__(self, sqlite_path: str, image_root: str | None = None,
                 image_base_uri: str = "", project_id: int | None = None):
        self.sqlite_path = Path(sqlite_path)
        self.image_root = Path(image_root) if image_root else None
        self.image_base_uri = image_base_uri.rstrip("/")
        self.project_id = project_id
        if not self.sqlite_path.is_file():
            raise FileNotFoundError(f"dump não encontrado: {sqlite_path}")

    def _connect(self) -> sqlite3.Connection:
        # somente leitura: o dump do usuário não é para ser tocado
        conn = sqlite3.connect(f"file:{self.sqlite_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def describe(self) -> str:
        return (f"Label Studio (sqlite) {self.sqlite_path.name}"
                + (f", projeto {self.project_id}" if self.project_id else ""))

    def classes(self) -> list[str]:
        """Classes declaradas no template de rotulagem do projeto."""
        with self._connect() as conn:
            sql = "SELECT label_config FROM project"
            params: tuple = ()
            if self.project_id is not None:
                sql += " WHERE id = ?"
                params = (self.project_id,)
            rows = conn.execute(sql, params).fetchall()
        out: list[str] = []
        for row in rows:
            for name in LABEL_RE.findall(row["label_config"] or ""):
                if name not in out:
                    out.append(name)
        return out

    def read(self, report: ImportReport) -> Iterator[SourceSample]:
        with self._connect() as conn:
            sql = ("SELECT t.id, t.data,"
                   "       (SELECT group_concat(c.result, '\x1e')"
                   "          FROM task_completion c"
                   "         WHERE c.task_id = t.id"
                   "           AND COALESCE(c.was_cancelled, 0) = 0) AS results,"
                   "       (SELECT u.email FROM task_completion c"
                   "          LEFT JOIN htx_user u ON u.id = c.completed_by_id"
                   "         WHERE c.task_id = t.id LIMIT 1) AS author"
                   "  FROM task t")
            params: tuple = ()
            if self.project_id is not None:
                sql += " WHERE t.project_id = ?"
                params = (self.project_id,)
            rows = conn.execute(sql + " ORDER BY t.id", params).fetchall()

        for row in rows:
            rel = _task_path(row["data"])
            if rel is None:
                report.skip(f"task:{row['id']}",
                            "não achei caminho de imagem em task.data")
                continue

            uri = (rel if "://" in rel
                   else f"{self.image_base_uri}/{rel}" if self.image_base_uri
                   else rel)
            local = None
            if self.image_root is not None and "://" not in rel:
                candidate = self.image_root / rel
                # A raiz local do LS costuma incluir o diretório do storage;
                # se não bater, tenta só o nome do arquivo.
                if not candidate.is_file():
                    alt = self.image_root / Path(rel).name
                    candidate = alt if alt.is_file() else candidate
                local = str(candidate) if candidate.is_file() else None
                if local is None:
                    report.skip(rel, "arquivo não encontrado em image_root")
                    continue

            boxes: list[SourceBox] = []
            width = height = None
            annotated = bool(row["results"])

            for chunk in (row["results"] or "").split("\x1e"):
                if not chunk.strip():
                    continue
                try:
                    items = json.loads(chunk)
                except json.JSONDecodeError:
                    report.skip(rel, "result do LS não é JSON válido")
                    continue
                for item in items or []:
                    if item.get("type") != "rectanglelabels":
                        report.warn(
                            f"tipo '{item.get('type')}' ignorado — só "
                            f"rectanglelabels vira bbox")
                        continue
                    value = item.get("value") or {}
                    names = value.get("rectanglelabels") or []
                    if not names:
                        continue
                    if float(value.get("rotation") or 0):
                        # YOLO axis-aligned não representa caixa girada;
                        # importar ignorando a rotação daria caixa errada.
                        report.skip(rel, "bbox com rotação — não suportado")
                        continue
                    width = width or item.get("original_width")
                    height = height or item.get("original_height")
                    cx, cy, w, h = _to_yolo(value)
                    boxes.append(SourceBox(cls_name=names[0], cx=cx, cy=cy,
                                           w=w, h=h))

            yield SourceSample(key=rel, uri=uri, local_path=local,
                               width=width, height=height, boxes=boxes,
                               annotated=annotated,
                               author_email=row["author"],
                               metadata={"ls_task_id": row["id"]})
