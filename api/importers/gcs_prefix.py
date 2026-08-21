"""Importador de um prefixo de bucket GCS — a origem principal de imagens.

Diferença essencial para o importador de arquivo local: **não baixa nada**. A
listagem do GCS já traz `md5Hash`, `size` e `contentType` de cada objeto, então
catalogar 500 mil imagens custa algumas chamadas de listagem em vez de
centenas de gigabytes de download.

O preço é não saber largura/altura sem abrir o arquivo. Como isso só é
necessário na hora de treinar (e o treino baixa a imagem de qualquer forma),
elas ficam nulas até alguém precisar — anotar já resolve, porque a tela de
anotação abre a imagem.
"""
from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path

from .. import storage
from .base import ImportReport, SourceSample

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


class GCSPrefixImporter:
    kind = "gcs_prefix"

    def __init__(self, bucket: str, prefix: str = "", limit: int | None = None):
        self.bucket = bucket.replace("gs://", "").strip("/")
        # O prefixo vai como veio, SEM remover a barra inicial: nome de objeto
        # no GCS é uma string opaca e pode legitimamente começar com "/". O
        # bucket de produção deste projeto é assim ("/frame_2024...") — remover
        # a barra fazia a listagem devolver zero.
        self.prefix = prefix
        self.limit = limit

    def describe(self) -> str:
        return f"gs://{self.bucket}/{self.prefix}"

    def classes(self) -> list[str]:
        """Um prefixo de bucket traz imagens, não anotações."""
        return []

    def read(self, report: ImportReport) -> Iterator[SourceSample]:
        client = storage.cliente()
        n = 0
        for blob in client.list_blobs(self.bucket, prefix=self.prefix):
            if blob.name.endswith("/"):
                continue
            if Path(blob.name).suffix.lower() not in IMG_EXTS:
                continue
            if not blob.md5_hash:
                # Objeto composto ou com upload sem checksum: sem md5 não dá
                # para deduplicar, e importar sem identidade criaria duplicata
                # silenciosa a cada reimportação.
                report.skip(blob.name, "objeto sem md5 no GCS")
                continue

            yield SourceSample(
                key=blob.name,
                uri=f"gs://{self.bucket}/{blob.name}",
                local_path=None,
                md5=base64.b64decode(blob.md5_hash),
                bytes_size=blob.size,
                boxes=[], annotated=False,
                metadata={"gcs_generation": blob.generation,
                          "content_type": blob.content_type,
                          "updated": blob.updated.isoformat()
                          if blob.updated else None})
            n += 1
            if self.limit and n >= self.limit:
                report.warn(f"parou em {self.limit} imagens (limite pedido)")
                return
