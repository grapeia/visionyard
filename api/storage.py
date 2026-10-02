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


def como_assinar(c: Any) -> dict[str, str] | None:
    """O que `generate_signed_url` precisa receber para assinar com esta
    credencial, ou None se ela não assina.

    Chave de conta de serviço assina sozinha (`{}`). A credencial da VM ou do
    Cloud Run não tem chave privada nem `signer_email`, e sem argumento nenhum
    a biblioteca desiste com "you need a private key": ela só assina pelo IAM
    (`signBlob`, que a conta precisa ter sobre si mesma) quando recebe o e-mail
    da conta e um token. O e-mail vem "default" até o primeiro refresh, que é
    quem o resolve. ADC de usuário não assina de jeito nenhum.
    """
    cred: Any = getattr(c, "_credentials", None)
    if hasattr(cred, "signer_email"):
        return {}
    if not hasattr(cred, "service_account_email"):
        return None
    if not cred.valid or cred.service_account_email == "default":
        from google.auth.transport.requests import Request
        cred.refresh(Request())
    return {"service_account_email": cred.service_account_email,
            "access_token": cred.token}


def assinar_ou_falhar(uris: list[str], minutos: int, metodo: str = "GET",
                      content_type: str | None = None) -> dict[str, str]:
    """URL assinada para cada `gs://`, para o AGENTE — sem meio-termo.

    O contrário de `assinar()`: lá a URL que falta vira um quadrado cinza na
    galeria; aqui ela vira um treino que falha no meio, então qualquer falha
    sobe. `content_type` entra na assinatura do PUT, e o agente precisa
    mandar o mesmo cabeçalho — o GCS recusa o upload se não bater.
    """
    c = cliente()
    assinante = como_assinar(c)
    if assinante is None:
        raise RuntimeError("a credencial da API não assina URL — é de usuário, "
                           "não de conta de serviço")
    out: dict[str, str] = {}
    for uri in uris:
        if not uri.startswith("gs://"):
            raise ValueError(f"esperava gs://, veio {uri!r}")
        bucket, _, chave = uri[5:].partition("/")
        kw: dict[str, Any] = {"version": "v4", "method": metodo,
                              "expiration": timedelta(minutes=minutos),
                              **assinante}
        if content_type:
            kw["content_type"] = content_type
        out[uri] = c.bucket(bucket).blob(chave).generate_signed_url(**kw)
    return out


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
        assinante = como_assinar(c)
    except Exception:
        return {}
    if assinante is None:
        return {}
    out: dict[str, str] = {}
    for uri in uris:
        if not uri.startswith("gs://"):
            continue
        bucket, _, chave = uri[5:].partition("/")
        try:
            out[uri] = c.bucket(bucket).blob(chave).generate_signed_url(
                version="v4", expiration=timedelta(minutes=minutos),
                method="GET", **assinante)
        except Exception:
            continue
    return out
