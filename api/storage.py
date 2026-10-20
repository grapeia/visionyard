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


# `cloud-platform`, e não os `devstorage.*` que o `storage.Client()` pede
# sozinho: na VM e no Cloud Run o token vem do servidor de metadados com os
# escopos pedidos, e é com ele que `como_assinar` chama o `signBytes` do IAM —
# que recusa token só de storage com 403 "insufficient authentication scopes".
# Chave exportada assina localmente e não passa por aí.
ESCOPO = "https://www.googleapis.com/auth/cloud-platform"


def _construir() -> Any:
    import google.auth
    from google.cloud import storage
    cred, projeto = google.auth.default(scopes=[ESCOPO])
    if not projeto:
        # A chave que o gcloud exporta nem sempre traz project_id; deduz pelo
        # domínio do client_email da conta de serviço. Sem isto, instalação
        # que autentica por chave exportada não lista bucket nenhum.
        chave = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if chave and Path(chave).is_file():
            dados = json.loads(Path(chave).read_text())
            email = dados.get("client_email") or ""
            projeto = dados.get("project_id") or (
                email.split("@", 1)[1].split(".", 1)[0] if "@" in email else "")
    return storage.Client(credentials=cred, project=projeto or None)


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


# O que cada papel de bucket precisa: o de imagens é lido (galeria e agente),
# o de modelos é gravado no fim do treino e lido no pré-tag, export e deploy.
PERMISSOES_IMAGENS = ["storage.objects.list", "storage.objects.get"]
PERMISSOES_MODELOS = ["storage.objects.create", "storage.objects.get"]


def _falha(e: Exception) -> str:
    """Erro do fornecedor encurtado. Só vai para a tela do superusuário — a
    que existe justamente para dizer o que está errado na credencial —, e
    não para `job.error` ou rota que um viewer leia."""
    return f"{type(e).__name__}: {str(e)[:200]}"


# Os dois erros que quem configura mais vê, em português. O resto vai cru
# (encurtado): é diagnóstico, e esconder o motivo não ajuda ninguém.
FALHAS_DE_BUCKET = {
    "NotFound": "bucket não existe — confira o nome",
    "Forbidden": "a conta não tem acesso a este bucket",
}


def diagnosticar(buckets: list[str], models_bucket: str) -> dict[str, Any]:
    """O que a credencial da API consegue fazer, sem gravar nada.

    `test_iam_permissions` responde quais das permissões pedidas a conta tem
    naquele bucket — inclusive a de criar objeto — sem criar objeto nenhum, e
    não exige permissão de ler a política do bucket.
    """
    out: dict[str, Any] = {"conta": None, "origem": None, "assina": False,
                           "erro": None, "buckets": []}
    try:
        c = cliente()
    except Exception as e:
        out["erro"] = _falha(e)
        return out
    cred: Any = getattr(c, "_credentials", None)
    if hasattr(cred, "signer_email"):
        out["origem"] = "chave de conta de serviço"
    elif hasattr(cred, "service_account_email"):
        out["origem"] = "identidade da máquina (VM/Cloud Run)"
    else:
        out["origem"] = "credencial de usuário — não assina URL"
    # Assina de verdade uma URL qualquer, em vez de só olhar a credencial: na
    # VM o que decide é a permissão `signBlob`, e ela só aparece tentando. A
    # assinatura não toca o objeto — ele nem precisa existir.
    try:
        assinante = como_assinar(c)
        if assinante is not None:
            alvo = models_bucket or (buckets[0] if buckets else "diagnostico")
            c.bucket(alvo).blob(".visionyard-diagnostico").generate_signed_url(
                version="v4", expiration=timedelta(minutes=1), method="GET",
                **assinante)
            out["assina"] = True
    except Exception as e:
        out["erro"] = _falha(e)
    out["conta"] = (getattr(cred, "signer_email", None)
                    or getattr(cred, "service_account_email", None))

    alvos = [(b, "imagens", PERMISSOES_IMAGENS) for b in buckets]
    if models_bucket:
        alvos.append((models_bucket, "modelos", PERMISSOES_MODELOS))
    for nome, papel, pedidas in alvos:
        item: dict[str, Any] = {"bucket": nome, "papel": papel,
                                "faltando": [], "erro": None}
        try:
            tem = set(c.bucket(nome).test_iam_permissions(pedidas))
            item["faltando"] = [p for p in pedidas if p not in tem]
        except Exception as e:
            item["erro"] = FALHAS_DE_BUCKET.get(type(e).__name__) or _falha(e)
        out["buckets"].append(item)
    return out


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


def assinar_download(uri: str, minutos: int, nome: str) -> str:
    """URL assinada que o NAVEGADOR segue para baixar um objeto, como `nome`.

    O nome vai na assinatura (`response-content-disposition`): sem ele o
    arquivo sai como a folha do caminho, e todo peso se chama `best.pt`.
    Levanta se não der — quem chama decide o que o usuário vê."""
    if not uri.startswith("gs://"):
        raise ValueError(f"esperava gs://, veio {uri!r}")
    c = cliente()
    assinante = como_assinar(c)
    if assinante is None:
        raise RuntimeError("a credencial da API não assina URL")
    bucket, _, chave = uri[5:].partition("/")
    url: str = c.bucket(bucket).blob(chave).generate_signed_url(
        version="v4", expiration=timedelta(minutes=minutos), method="GET",
        response_disposition=f'attachment; filename="{nome}"', **assinante)
    return url


class GrandeDemais(Exception):
    """O objeto passa do teto que quem chamou aceita baixar."""


def ler(uri: str, max_bytes: int) -> bytes:
    """Os bytes de um `gs://`, pela credencial da API, até `max_bytes`.

    O teto é conferido no metadado, antes de baixar: sem ele um objeto
    descomunal iria inteiro para a memória da API. Levanta se não der — quem
    chama decide o que o usuário vê (o texto do GCS cita a conta)."""
    if not uri.startswith("gs://"):
        raise ValueError(f"esperava gs://, veio {uri!r}")
    bucket, _, chave = uri[5:].partition("/")
    blob = cliente().bucket(bucket).get_blob(chave)
    if blob is None:
        raise FileNotFoundError(uri)
    if blob.size is not None and blob.size > max_bytes:
        raise GrandeDemais(f"{blob.size} bytes")
    return blob.download_as_bytes()


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
