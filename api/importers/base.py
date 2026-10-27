"""Contrato dos importadores.

Uma importação é uma **feature**, não uma migração de uma vez: fotos novas
entram no bucket toda semana e anotações chegam de origens diferentes. Por isso
todo importador obedece às mesmas regras:

  * **idempotente** — a mesma origem importada duas vezes não duplica nada.
    Sample é deduplicado por `sha256`; anotação, por (sample, autor).
  * **dry-run** — dá para ver o que aconteceria antes de gravar.
  * **mapeamento explícito de classes** — classe da origem que não casa com o
    LabelSchema do projeto é *reportada*, nunca descartada em silêncio.
  * **relatório persistido** — o que entrou, o que foi pulado e por quê.

Cada origem implementa `read()`, que só *lê e normaliza*. Gravar é
responsabilidade do runner, igual para todos — assim uma origem nova não pode
introduzir um jeito diferente de escrever no banco.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class SourceBox:
    """Um retângulo já normalizado para YOLO (centro, 0..1)."""
    cls_name: str
    cx: float
    cy: float
    w: float
    h: float


@dataclass
class SourcePolygon:
    """Contorno normalizado no quadro 0..1 da imagem."""
    cls_name: str
    pts: list[tuple[float, float]]


@dataclass
class SourceSample:
    """Uma imagem da origem, com suas anotações (se houver).

    `local_path` é de onde ler os bytes para calcular sha256 e dimensões;
    `uri` é onde a imagem vive de verdade (o que fica gravado no banco).
    Podem divergir: importar de um dump local cujos arquivos já estão no GCS.
    """
    key: str                       # identificador na origem (p/ diagnóstico)
    uri: str
    local_path: str | None = None
    # Identidade de conteúdo. Quando a origem já a fornece (o GCS traz md5Hash
    # na listagem), não há motivo para baixar a imagem só para calcular hash.
    md5: bytes | None = None
    bytes_size: int | None = None
    width: int | None = None
    height: int | None = None
    boxes: list[SourceBox] = field(default_factory=list)
    polygons: list[SourcePolygon] = field(default_factory=list)
    annotation_incomplete: bool = False
    annotated: bool = False        # False = imagem sem anotação na origem
    author_email: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ImportReport:
    """Contagens + os motivos. As listas são truncadas para não estourar o
    jsonb; `*_total` guarda o número real."""
    samples_created: int = 0
    samples_existing: int = 0
    annotations_created: int = 0
    annotations_updated: int = 0
    skipped: list[dict] = field(default_factory=list)
    skipped_total: int = 0
    unmapped_classes: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    MAX_DETAIL = 50

    def skip(self, key: str, reason: str) -> None:
        self.skipped_total += 1
        if len(self.skipped) < self.MAX_DETAIL:
            self.skipped.append({"key": key, "reason": reason})

    def warn(self, message: str) -> None:
        if message not in self.warnings and len(self.warnings) < self.MAX_DETAIL:
            self.warnings.append(message)

    def as_dict(self) -> dict:
        d = {"samples_created": self.samples_created,
             "samples_existing": self.samples_existing,
             "annotations_created": self.annotations_created,
             "annotations_updated": self.annotations_updated,
             "skipped_total": self.skipped_total,
             "skipped": self.skipped,
             "unmapped_classes": self.unmapped_classes,
             "warnings": self.warnings}
        if self.skipped_total > len(self.skipped):
            d["skipped_truncado"] = (
                f"mostrando {len(self.skipped)} de {self.skipped_total}")
        return d


class Importer(Protocol):
    """Lê uma origem e devolve samples normalizados.

    Não toca no banco: quem grava é o runner.
    """

    kind: str

    def describe(self) -> str:
        """Uma linha para o log do job."""

    def classes(self) -> list[str]:
        """Classes que a origem declara — usado para conferir o mapeamento
        antes de importar qualquer coisa."""

    def read(self, report: ImportReport) -> Iterator[SourceSample]:
        """Itera os samples. Problemas de leitura vão para `report.skip()`,
        não levantam exceção: uma imagem ilegível não pode abortar 10 mil."""


def map_classes(source_classes: list[str], schema_classes: list[str],
                overrides: dict[str, str] | None = None
                ) -> tuple[dict[str, int], list[str]]:
    """Casa classe da origem com índice do LabelSchema.

    Por nome exato; `overrides` resolve o resto ({"origem_a": "classe_a"}).
    Devolve (mapa, não-mapeadas). O chamador decide o que fazer com as
    não-mapeadas — o importador nunca as descarta calado.
    """
    overrides = overrides or {}
    index = {name: i for i, name in enumerate(schema_classes)}
    mapping: dict[str, int] = {}
    unmapped: list[str] = []
    for name in source_classes:
        target = overrides.get(name, name)
        if target in index:
            mapping[name] = index[target]
        else:
            unmapped.append(name)
    return mapping, unmapped
