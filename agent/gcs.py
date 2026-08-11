"""Acesso ao GCS pelo agente — com credencial própria (ADC), não URL assinada.

O browser recebe URL assinada porque é não-confiável; o agente é infraestrutura
e usa as mesmas credenciais que a API já usa (GOOGLE_APPLICATION_CREDENTIALS).
Assim a API não precisa assinar milhares de URLs por treino.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from google.cloud import storage

_client: storage.Client | None = None


def _cli() -> storage.Client:
    """Cliente GCS. A chave exportada pelo gcloud (legacy_credentials) não traz
    project_id, então storage.Client() sozinho falha com 'Project was not
    passed'. Mesma dedução que a API faz: o project sai do e-mail da service
    account (<nome>@<project>.iam...)."""
    global _client
    if _client is None:
        try:
            _client = storage.Client()
        except Exception:  # noqa: BLE001
            projeto = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
            chave = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
            if not projeto and chave and Path(chave).is_file():
                dados = json.loads(Path(chave).read_text())
                email = dados.get("client_email") or ""
                projeto = dados.get("project_id") or (
                    email.split("@", 1)[1].split(".", 1)[0] if "@" in email else "")
            if not projeto:
                raise
            _client = storage.Client(project=projeto)
    return _client


def _split(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"esperava gs://, veio {uri!r}")
    bucket, _, chave = uri[5:].partition("/")
    return bucket, chave


def baixar(uri: str, destino: Path) -> None:
    bucket, chave = _split(uri)
    destino.parent.mkdir(parents=True, exist_ok=True)
    _cli().bucket(bucket).blob(chave).download_to_filename(str(destino))


def enviar(origem: Path, uri: str) -> None:
    bucket, chave = _split(uri)
    _cli().bucket(bucket).blob(chave).upload_from_filename(str(origem))


def sha256_arquivo(caminho: Path) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()
