"""Acesso ao GCS: uma porta só.

Quatro lugares construíam o cliente por conta própria — `routers/buckets.py`,
`routers/samples.py`, `importers/gcs_prefix.py` — com a mesma dedução de
`project_id` copiada em dois deles, e a `samples.py` importava o `_client`
privado da `buckets.py`, o único import cruzado entre routers da árvore.

Além de juntar isso, a porta muda duas coisas: o cliente passa a ser
construído uma vez e não a cada requisição, e a assinatura de URL vira código
exercitável — antes ela só rodava com credencial de verdade, então o que a
suíte cobria era o caminho em que ela desiste.

O que NÃO está aqui, de propósito: a navegação de bucket (`buckets.py`) segue
assinando pelo próprio blob que a listagem já devolveu, porque passar por
`assinar()` custaria uma segunda ida ao GCS por miniatura.
"""
from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

_cliente: Any = None


def _construir() -> Any:
    from google.cloud import storage
    try:
        return storage.Client()
    except Exception:
        # A chave que o gcloud exporta nem sempre traz project_id; deduz pelo
        # domínio do client_email da conta de serviço. Sem isto, instalação
        # que autentica por chave exportada não lista bucket nenhum.
        projeto = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        chave = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if not projeto and chave and Path(chave).is_file():
            dados = json.loads(Path(chave).read_text())
            email = dados.get("client_email") or ""
            projeto = dados.get("project_id") or (
                email.split("@", 1)[1].split(".", 1)[0] if "@" in email else "")
        if not projeto:
            raise
        return storage.Client(project=projeto)


def cliente() -> Any:
    """O cliente GCS da instalação, construído uma vez.

    Só o sucesso fica guardado: erro volta a tentar na chamada seguinte, senão
    uma credencial que apareceu depois (secret montado tarde, máquina que
    ganhou a conta de serviço) nunca seria vista sem reiniciar o processo.

    Duas threads podem construir ao mesmo tempo e uma sobrescrever a outra —
    sem consequência: o cliente é intercambiável e a corrida só desperdiça uma
    construção.
    """
    global _cliente
    if _cliente is None:
        _cliente = _construir()
    return _cliente


def pode_assinar(c: Any) -> bool:
    """Conta de serviço assina; ADC de usuário não tem chave privada."""
    return hasattr(getattr(c, "_credentials", None), "signer_email")


def assinar(uris: list[str], minutos: int) -> dict[str, str]:
    """URL assinada para cada `gs://`, para o navegador buscar direto do GCS.

    Devolve o que conseguiu: sem credencial, sem credencial que assine, ou com
    um objeto que a conta não alcança, a chave simplesmente não aparece no
    dicionário — e é essa ausência que a tela lê como "mostre o lugar da
    imagem". Falhar aqui apagaria a página inteira de miniaturas por causa de
    um objeto.
    """
    try:
        c = cliente()
    except Exception:
        return {}
    if not pode_assinar(c):
        return {}
    out: dict[str, str] = {}
    for uri in uris:
        if not uri.startswith("gs://"):
            continue
        bucket, _, chave = uri[5:].partition("/")
        try:
            out[uri] = c.bucket(bucket).blob(chave).generate_signed_url(
                version="v4", expiration=timedelta(minutes=minutos),
                method="GET")
        except Exception:
            continue
    return out
