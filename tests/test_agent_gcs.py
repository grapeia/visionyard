"""Transferência do agente por URL assinada — sem rede, sem GCS.

O que importa aqui é o que a troca do SDK pelo HTTP podia quebrar em silêncio:
download cortado não pode virar imagem de cache, 4xx não se repete (URL
expirada continua expirada), e a assinatura vai em lote.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import requests

from agent import gcs


class _Resp:
    def __init__(self, status: int, corpo: bytes = b"", quebra: bool = False):
        self.status_code = status
        self._corpo = corpo
        self._quebra = quebra
        self.text = corpo.decode(errors="replace")
        self.content = corpo

    def iter_content(self, chunk_size: int):
        yield self._corpo[:2]
        if self._quebra:
            raise requests.exceptions.ChunkedEncodingError("conexão caiu")
        yield self._corpo[2:]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def sem_espera(monkeypatch):
    monkeypatch.setattr(gcs.time, "sleep", lambda s: None)


def _get_em_sequencia(monkeypatch, respostas: list[_Resp]) -> list[str]:
    pedidos: list[str] = []

    def get(url, **kw):
        pedidos.append(url)
        return respostas.pop(0)

    monkeypatch.setattr(gcs.requests, "get", get)
    return pedidos


def test_download_cortado_nao_deixa_arquivo(tmp_path, monkeypatch):
    """O cache decide por `is_file()`: meio arquivo ali seria imagem
    corrompida em todo treino seguinte."""
    _get_em_sequencia(monkeypatch, [_Resp(200, b"abcdef", quebra=True)] * 4)
    destino = tmp_path / "img.jpg"
    with pytest.raises(RuntimeError):
        gcs.baixar_url("https://u", destino)
    assert not destino.exists()
    assert list(tmp_path.iterdir()) == []


def test_download_repete_depois_de_corte(tmp_path, monkeypatch):
    _get_em_sequencia(monkeypatch, [_Resp(200, b"abcdef", quebra=True),
                                    _Resp(503), _Resp(200, b"abcdef")])
    destino = tmp_path / "img.jpg"
    gcs.baixar_url("https://u", destino)
    assert destino.read_bytes() == b"abcdef"


def test_4xx_nao_repete_e_so_o_codigo_sai(tmp_path, monkeypatch):
    """O erro vai para `job.error`, que um viewer lê: o pedido canônico do
    SignatureDoesNotMatch cita a conta de serviço da API."""
    corpo = (b"<Error><Code>SignatureDoesNotMatch</Code><StringToSign>"
             b"GOOG4-RSA-SHA256 sa-api@projeto.iam</StringToSign></Error>")
    pedidos = _get_em_sequencia(monkeypatch, [_Resp(403, corpo)])
    with pytest.raises(RuntimeError) as e:
        gcs.baixar_url("https://u?X-Goog-Signature=segredo", tmp_path / "x")
    assert len(pedidos) == 1
    assert "SignatureDoesNotMatch" in str(e.value)
    assert "sa-api" not in str(e.value)
    assert "segredo" not in str(e.value)


def test_erro_de_conexao_nao_vaza_a_url_assinada(tmp_path, monkeypatch):
    """A exceção do requests cita a URL com a query — e a query é a
    credencial. Num upload, um PUT ainda válido sobre o peso."""
    def put(url, **kw):
        raise requests.exceptions.ConnectionError(
            f"Max retries exceeded with url: {url}")

    monkeypatch.setattr(gcs.requests, "put", put)
    arq = tmp_path / "a.pt"
    arq.write_bytes(b"x")
    with pytest.raises(RuntimeError) as e:
        gcs.enviar_url("https://put?X-Goog-Signature=segredo", arq, {})
    assert "segredo" not in str(e.value)
    assert "ConnectionError" in str(e.value)


class _Cli:
    def __init__(self, omitir: str | None = None):
        self.lotes: list[list[str]] = []
        self.omitir = omitir

    def assinar(self, job_id, uris):
        self.lotes.append(uris)
        return {u: f"https://assinada/{u[5:]}" for u in uris if u != self.omitir}

    def destino_upload(self, job_id, arquivo):
        return {"uri": f"gs://m/{arquivo}", "url": "https://put",
                "headers": {"Content-Type": "application/octet-stream"}}


def test_baixar_lote_assina_em_lotes(tmp_path, monkeypatch):
    monkeypatch.setattr(gcs, "LOTE", 2)
    monkeypatch.setattr(gcs, "baixar_url",
                        lambda url, destino, o_que="": destino.write_text(url))
    cli = _Cli()
    pares = [(f"gs://b/{i}.jpg", tmp_path / f"{i}.jpg") for i in range(5)]
    vistos: list[int] = []
    gcs.baixar_lote(cli, "j", pares, vistos.append)
    assert [len(lote) for lote in cli.lotes] == [2, 2, 1]
    assert vistos == [2, 4, 5]
    assert (tmp_path / "3.jpg").read_text() == "https://assinada/b/3.jpg"


def test_baixar_lote_sem_url_falha(tmp_path, monkeypatch):
    monkeypatch.setattr(gcs, "baixar_url", lambda *a, **kw: None)
    cli = _Cli(omitir="gs://b/1.jpg")
    with pytest.raises(RuntimeError, match=re.escape("gs://b/1.jpg")):
        gcs.baixar_lote(cli, "j", [(f"gs://b/{i}.jpg", tmp_path / f"{i}")
                                   for i in range(2)])


def test_publicar_manda_o_cabecalho_assinado(tmp_path, monkeypatch):
    """O Content-Type entra na assinatura do PUT: sem ele o GCS recusa."""
    enviados: list[dict] = []

    def put(url, data, headers, timeout):
        enviados.append({"url": url, "headers": headers, "corpo": data.read()})
        return _Resp(200)

    monkeypatch.setattr(gcs.requests, "put", put)
    peso = tmp_path / "best.pt"
    peso.write_bytes(b"pesos")
    uri = gcs.publicar(_Cli(), "j", peso)
    assert uri == "gs://m/best.pt"
    assert enviados == [{"url": "https://put", "corpo": b"pesos",
                         "headers": {"Content-Type": "application/octet-stream"}}]


def test_upload_repete_do_inicio_do_arquivo(tmp_path, monkeypatch):
    corpos: list[bytes] = []

    def put(url, data, headers, timeout):
        corpos.append(data.read())
        return _Resp(500 if len(corpos) == 1 else 200)

    monkeypatch.setattr(gcs.requests, "put", put)
    arq = tmp_path / "a.pt"
    arq.write_bytes(b"tudo")
    gcs.enviar_url("https://put", arq, {})
    assert corpos == [b"tudo", b"tudo"]


def test_agente_nao_importa_o_sdk_do_gcs():
    """A dependência saiu do requirements do agente; um import esquecido só
    apareceria na máquina, ao subir."""
    raiz = Path(gcs.__file__).parent
    for arq in raiz.glob("*.py"):
        assert "google.cloud" not in arq.read_text(), arq.name
