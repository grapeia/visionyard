"""Navegação de bucket: pastas, objetos e miniaturas.

Listar um prefixo amplo custa caro (o bucket de produção tem centenas de
milhares de objetos), então a navegação usa `delimiter="/"`: o GCS devolve
só o nível atual — as "pastas" logo abaixo e os objetos que estão nelas — em
vez de percorrer tudo. No bucket deste projeto o topo tem 35 pastas e sai em
~1,5 s.

As miniaturas vêm de URL assinada, que o navegador busca direto do GCS. Onde a
credencial não sabe assinar (ADC de usuário, sem chave privada), a lista
aparece sem imagem em vez de falhar.

Único serviço sem uma linha de SQL: o que ele guarda é a cerca (a allowlist de
buckets) e a forma da resposta. O acesso ao GCS em si é a porta de
`api/storage.py`.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status
from starlette.concurrency import run_in_threadpool

from .. import storage
from ..config import settings

logger = logging.getLogger(__name__)

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MAX_OBJETOS = 60
MAX_MINIATURAS = 24


def configurados() -> list[dict[str, str]]:
    """Buckets pré-definidos na instalação.

    Vem de configuração e não do banco: é infraestrutura do ambiente, muda
    junto com o deploy, e digitar o nome à mão toda vez convida a erro de
    digitação que só aparece como 'nenhuma imagem encontrada'.
    """
    return [{"nome": b} for b in settings.buckets]


def _navegar(bucket: str, prefixo: str) -> dict:
    cliente = storage.cliente()
    iterador = cliente.list_blobs(bucket, prefix=prefixo, delimiter="/",
                                  max_results=MAX_OBJETOS + 1)
    blobs = list(iterador)
    pastas = sorted(iterador.prefixes)

    tem_mais = len(blobs) > MAX_OBJETOS
    blobs = blobs[:MAX_OBJETOS]

    assina = storage.pode_assinar(cliente)
    objetos, miniaturas = [], 0
    for b in blobs:
        nome = b.name[len(prefixo):] if prefixo else b.name
        if not nome:
            # Objeto-placeholder de pasta: o console do GCS cria um objeto de
            # zero byte com o nome do próprio prefixo. Sem isso ele aparece
            # como uma linha sem nome.
            continue
        eh_imagem = Path(b.name).suffix.lower() in IMG_EXTS
        url = None
        if eh_imagem and assina and miniaturas < MAX_MINIATURAS:
            try:
                url = b.generate_signed_url(version="v4",
                                            expiration=timedelta(minutes=30),
                                            method="GET")
                miniaturas += 1
            except Exception:      # sem assinatura, segue sem imagem
                url = None
        objetos.append({"nome": nome, "caminho": b.name, "bytes": b.size,
                        "imagem": eh_imagem, "url": url})

    # sobe um nível: "A/B/C/" -> "A/B/"
    pai = None
    if prefixo:
        partes = prefixo.rstrip("/").split("/")
        pai = "/".join(partes[:-1]) + "/" if len(partes) > 1 else ""

    return {"bucket": bucket, "prefixo": prefixo, "pai": pai,
            "pastas": pastas, "objetos": objetos, "tem_mais": tem_mais,
            "imagens": sum(1 for o in objetos if o["imagem"]),
            "assina": assina}


async def navegar(bucket: str, prefixo: str) -> dict[str, Any]:
    bucket = bucket.replace("gs://", "").strip("/")

    # Lista vazia nega tudo, em vez de liberar tudo. A conta de serviço desta
    # máquina enxerga o projeto inteiro do GCP, então uma allowlist que falha
    # aberta transformaria a rota num listador de qualquer bucket da empresa.
    if bucket not in settings.buckets:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bucket não disponível")

    try:
        return await run_in_threadpool(_navegar, bucket, prefixo)
    except HTTPException:
        raise
    except Exception as e:
        # O detalhe fica no log do servidor: erro do GCS costuma citar
        # projeto, conta de serviço e caminho interno, que não têm por que
        # chegar ao navegador.
        logger.warning("falha ao navegar gs://%s/%s: %s: %s",
                       bucket, prefixo, type(e).__name__, e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "não consegui ler o bucket") from e
