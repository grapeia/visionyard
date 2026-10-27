"""A porta do GCS.

Antes dela, quatro lugares construíam o cliente por conta própria e a
assinatura de URL não tinha como ser exercitada sem credencial de verdade —
o que a suíte cobria era só o caminho em que ela desiste. Com a porta, o
cliente é um parâmetro do teste.
"""
from __future__ import annotations

from api import storage


class _Assinante:
    signer_email = "conta@projeto.iam.gserviceaccount.com"


class _Blob:
    def __init__(self, chave: str):
        self.chave = chave

    def generate_signed_url(self, **kw) -> str:
        if "quebrada" in self.chave:
            raise RuntimeError("objeto sem permissão")
        return f"https://assinada/{self.chave}?v={kw['version']}"


class _Bucket:
    def blob(self, chave: str) -> _Blob:
        return _Blob(chave)


class _Cliente:
    def __init__(self, assina: bool = True):
        self._credentials = _Assinante() if assina else object()

    def bucket(self, nome: str) -> _Bucket:
        return _Bucket()


def test_sem_credencial_que_assina_devolve_vazio(monkeypatch):
    """ADC de usuário não tem chave privada. A galeria mostra o lugar da
    imagem em vez de quebrar — é a ausência da URL que ela lê."""
    monkeypatch.setattr(storage, "cliente", lambda: _Cliente(assina=False))
    assert storage.assinar(["gs://b/a.jpg"], 15, ["b"]) == {}


def test_sem_credencial_nenhuma_devolve_vazio(monkeypatch):
    def explode():
        raise RuntimeError("sem credencial nesta máquina")

    monkeypatch.setattr(storage, "cliente", explode)
    assert storage.assinar(["gs://b/a.jpg"], 15, ["b"]) == {}


def test_assina_so_o_que_e_gs(monkeypatch):
    monkeypatch.setattr(storage, "cliente", lambda: _Cliente())
    out = storage.assinar(["gs://b/a.jpg", "/local/b.jpg", "https://x/c.jpg"], 15,
                          ["b"])
    assert list(out) == ["gs://b/a.jpg"]


def test_falha_em_uma_nao_leva_as_outras(monkeypatch):
    """Uma imagem sem permissão não pode apagar a página inteira de miniaturas."""
    monkeypatch.setattr(storage, "cliente", lambda: _Cliente())
    out = storage.assinar(["gs://b/quebrada.jpg", "gs://b/boa.jpg"], 15, ["b"])
    assert list(out) == ["gs://b/boa.jpg"]


def test_nao_assina_bucket_fora_da_allowlist(monkeypatch):
    """Imagem importada antes da cerca do runner pode apontar para outro
    bucket que a conta de serviço lê. Sem URL, a tela mostra o lugar dela."""
    monkeypatch.setattr(storage, "cliente", lambda: _Cliente())
    out = storage.assinar(["gs://b/a.jpg", "gs://alheio/x.jpg"], 15, ["b"])
    assert list(out) == ["gs://b/a.jpg"]


def test_cliente_e_construido_uma_vez(monkeypatch):
    """`samples.py` construía um cliente por requisição, dentro do threadpool,
    a cada carga da galeria."""
    chamadas = []

    def construir():
        chamadas.append(1)
        return _Cliente()

    monkeypatch.setattr(storage, "_cliente", None)
    monkeypatch.setattr(storage, "_construir", construir)
    assert storage.cliente() is storage.cliente()
    assert len(chamadas) == 1


def test_credencial_cobre_a_api_do_iam(monkeypatch):
    """Sem credencial explícita, `storage.Client()` pede só os escopos
    `devstorage.*`. No Cloud Run o servidor de metadados entrega o token com
    exatamente esses, e o `signBytes` do IAM — que é como a identidade da
    máquina assina URL — o recusa: 403 "insufficient authentication scopes",
    galeria sem miniatura e máquina sem download. `cloud-platform` cobre os
    dois."""
    import google.auth
    from google.cloud import storage as gcs

    pedidos: list = []
    recebido: dict = {}
    cred = object()

    def default(scopes=None, **kw):
        pedidos.append(scopes)
        return cred, "projeto-x"

    def construir_cliente(*a, **kw):
        recebido.update(kw)
        return "cliente"

    monkeypatch.setattr(google.auth, "default", default)
    monkeypatch.setattr(gcs, "Client", construir_cliente)
    assert storage._construir() == "cliente"
    assert pedidos == [["https://www.googleapis.com/auth/cloud-platform"]]
    assert recebido["credentials"] is cred
    assert recebido["project"] == "projeto-x"


def test_download_leva_o_nome_na_assinatura(monkeypatch):
    """Com a biblioteca de verdade e uma chave gerada na hora (assina local,
    sem rede): o nome do arquivo entra na URL assinada, senão todo peso
    baixaria como `best.pt`."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from google.cloud import storage as gcs
    from google.oauth2 import service_account

    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = chave.private_bytes(serialization.Encoding.PEM,
                              serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode()
    cred = service_account.Credentials.from_service_account_info({
        "type": "service_account", "client_email": "api@proj.iam.gserviceaccount.com",
        "private_key": pem, "token_uri": "https://oauth2.googleapis.com/token"})
    monkeypatch.setattr(storage, "cliente",
                        lambda: gcs.Client(credentials=cred, project="proj"))

    url = storage.assinar_download("gs://modelos/m/run/best.pt", 5, "detector-v1.pt")
    assert url.startswith("https://storage.googleapis.com/modelos/m/run/best.pt?")
    assert ("response-content-disposition="
            "attachment%3B%20filename%3D%22detector-v1.pt%22") in url
    assert "X-Goog-Expires=300" in url
