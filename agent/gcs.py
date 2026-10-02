"""Acesso ao bucket pelo agente — por URL assinada, sem credencial do GCS.

A máquina de execução não guarda conta de serviço: ela pede à API uma URL que
vale por minutos e vai direto ao GCS com ela. A API só assina o que o job
referencia (imagens da versão, o peso do payload) e escolhe ela mesma onde o
peso treinado é publicado — um dispositivo comprometido leva o que o job dele
já ia ler, e não o bucket inteiro.

Os bytes não passam pela API: ela assina, o GCS serve.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

import requests

from .client import Client

# Quantas URIs por pedido de assinatura. A API recusa acima de 500; menos que
# isso deixa a URL do fim do lote longe de expirar enquanto as primeiras baixam.
LOTE = 200

TENTATIVAS = 4
TIMEOUT = 60.0


def _codigo_gcs(corpo: str) -> str:
    """Só o `<Code>` do XML de erro do GCS (ExpiredToken, AccessDenied...).

    O resto do corpo não sai daqui: num SignatureDoesNotMatch ele traz o
    pedido canônico, com o e-mail da conta de serviço da API, e a mensagem
    vai parar em `job.error`, que um `viewer` lê."""
    m = re.search(r"<Code>([A-Za-z]{1,64})</Code>", corpo)
    return m.group(1) if m else "sem código"


def _com_tentativas(fazer: Callable[[], requests.Response], o_que: str) -> None:
    """Rede que pisca e 5xx do GCS repetem; 4xx não — URL expirada ou
    assinatura que não bate não melhoram tentando de novo.

    Nenhuma mensagem aqui leva o texto da exceção do `requests`: ele cita a
    URL com a query, e a query É a credencial — um PUT ainda válido para
    sobrescrever o peso, entregue a quem lê o erro do job."""
    ultimo = ""
    for i in range(TENTATIVAS):
        try:
            r = fazer()
        except requests.exceptions.RequestException as e:
            ultimo = type(e).__name__
        else:
            if r.status_code < 400:
                return
            ultimo = f"{r.status_code} {_codigo_gcs(r.text)}"
            if r.status_code < 500:
                raise RuntimeError(f"{o_que} → {ultimo}")
        if i < TENTATIVAS - 1:
            time.sleep(min(2 ** i, 8))
    raise RuntimeError(f"{o_que} falhou após {TENTATIVAS} tentativas: {ultimo}")


def baixar_url(url: str, destino: Path, o_que: str = "download") -> None:
    """Baixa para um temporário e só então renomeia: o cache de imagens é
    decidido por `is_file()`, e um download cortado no meio ficaria lá como
    imagem válida para todo treino seguinte."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_name(destino.name + ".parcial")

    def fazer() -> requests.Response:
        with requests.get(url, stream=True, timeout=TIMEOUT) as r:
            if r.status_code < 400:
                with open(tmp, "wb") as f:
                    for bloco in r.iter_content(chunk_size=1 << 20):
                        f.write(bloco)
            else:
                _ = r.content      # lê o corpo do erro antes de fechar
            return r

    try:
        _com_tentativas(fazer, o_que)
        os.replace(tmp, destino)
    finally:
        tmp.unlink(missing_ok=True)


def enviar_url(url: str, origem: Path, headers: dict[str, str],
               o_que: str = "upload") -> None:
    def fazer() -> requests.Response:
        with open(origem, "rb") as f:
            return requests.put(url, data=f, headers=headers, timeout=TIMEOUT)

    _com_tentativas(fazer, o_que)


def baixar_lote(cli: Client, job_id: str, pares: list[tuple[str, Path]],
                progresso: Callable[[int], None] | None = None) -> None:
    """Baixa cada `(uri, destino)`, pedindo a assinatura em lotes.

    Pede o lote na hora de baixá-lo, e não tudo no começo: com milhares de
    imagens as URLs do fim expirariam antes de a vez delas chegar."""
    feitas = 0
    for i in range(0, len(pares), LOTE):
        fatia = pares[i:i + LOTE]
        urls = cli.assinar(job_id, list(dict.fromkeys(u for u, _ in fatia)))
        for uri, destino in fatia:
            url = urls.get(uri)
            if url is None:
                raise RuntimeError(f"a API não devolveu URL para {uri}")
            baixar_url(url, destino, o_que=f"download de {uri}")
            feitas += 1
        if progresso is not None:
            progresso(feitas)


def baixar(cli: Client, job_id: str, uri: str, destino: Path) -> None:
    baixar_lote(cli, job_id, [(uri, destino)])


def publicar(cli: Client, job_id: str, origem: Path) -> str:
    """Sobe o arquivo onde a API mandar e devolve o `gs://` — que é o único
    que ela aceita registrar depois."""
    d = cli.destino_upload(job_id, origem.name)
    enviar_url(d["url"], origem, d.get("headers") or {},
               o_que=f"upload de {d['uri']}")
    return d["uri"]


def sha256_arquivo(caminho: Path) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()
