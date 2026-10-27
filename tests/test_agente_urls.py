"""O agente lê e grava no bucket por URL assinada, sem credencial do GCS.

Assinar é emitir credencial, então o que estes testes conferem é o escopo: a
API assina só o que o job referencia, grava só onde ela escolheu, e só aceita
registrar o que está debaixo desse caminho — senão o `weights_uri` registrado
viraria uma forma de fazê-la assinar a leitura de qualquer objeto depois.
"""
from __future__ import annotations

import psycopg
import pytest

from api import storage
from api.config import settings

from .conftest import TEST_DSN
from .test_datasets import DS, PROJ
from .test_models import _produzir_modelo
from .test_training import _criar_run, _device, _h, _publicar, _versao_congelada

pytestmark = pytest.mark.anyio


def _uris_da_versao() -> list[str]:
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        return [r[0] for r in conn.execute(
            "SELECT s.uri FROM dataset_version_item i"
            "  JOIN sample s ON s.id = i.sample_id ORDER BY s.uri").fetchall()]


async def _job_de_treino(admin, client, tmp_path) -> tuple[str, str]:
    await _versao_congelada(admin, tmp_path)
    await _criar_run(admin)
    tok = (await _device(admin, {"train": True}))["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    return tok, job["id"]


async def _urls(client, tok, jid, uris):
    return await client.post(f"/v1/agent/jobs/{jid}/urls", json={"uris": uris},
                             headers=_h(tok))


# --------------------------------------------------------------------------- #
# Leitura
# --------------------------------------------------------------------------- #

async def test_treino_le_as_imagens_da_versao(admin, client, tmp_path,
                                              assinaturas):
    tok, jid = await _job_de_treino(admin, client, tmp_path)
    uris = _uris_da_versao()
    assert uris
    r = await _urls(client, tok, jid, uris)
    assert r.status_code == 200, r.text
    assert set(r.json()["urls"]) == set(uris)
    assert all(m == "GET" for m, _ in assinaturas)


async def test_uri_fora_do_job_recusa_o_lote_inteiro(admin, client, tmp_path,
                                                     assinaturas):
    """Uma URI de fora e nada é assinado — nem as legítimas do mesmo pedido."""
    tok, jid = await _job_de_treino(admin, client, tmp_path)
    alheia = "gs://outro-cliente/segredo.pt"
    r = await _urls(client, tok, jid, [*_uris_da_versao(), alheia])
    assert r.status_code == 403
    assert alheia in r.json()["detail"]
    assert assinaturas == []


async def test_job_alheio_nao_assina(admin, client, tmp_path, assinaturas):
    _, jid = await _job_de_treino(admin, client, tmp_path)
    outra = (await _device(admin, {"train": True}, slug="outra"))["token"]
    r = await _urls(client, outra, jid, _uris_da_versao())
    assert r.status_code == 404
    assert assinaturas == []


async def test_job_encerrado_nao_ganha_url(admin, client, tmp_path):
    tok, jid = await _job_de_treino(admin, client, tmp_path)
    await client.post(f"/v1/agent/jobs/{jid}/finish",
                      json={"status": "failed"}, headers=_h(tok))
    r = await _urls(client, tok, jid, _uris_da_versao())
    assert r.status_code == 409
    r = await client.post(f"/v1/agent/jobs/{jid}/upload",
                          json={"arquivo": "best.pt"}, headers=_h(tok))
    assert r.status_code == 409


async def test_export_le_o_peso_e_nao_as_imagens(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/export",
        json={"format": "onnx"})
    tok = (await _device(admin, {"infer": True}, slug="exp"))["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    peso = job["payload"]["weights_uri"]

    r = await _urls(client, tok, job["id"], [peso])
    assert r.status_code == 200, r.text
    r = await _urls(client, tok, job["id"], [_uris_da_versao()[0]])
    assert r.status_code == 403


async def test_prelabel_le_peso_e_imagens_do_projeto(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel", json={
        "model_slug": "detector", "dataset_slug": DS["slug"],
        "only_missing": False})
    tok = (await _device(admin, {"prelabel": True}, slug="pre"))["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    alvos = (await client.get(f"/v1/agent/jobs/{job['id']}/targets",
                              headers=_h(tok))).json()["alvos"]
    uris = [job["payload"]["weights_uri"], *(a["uri"] for a in alvos)]
    r = await _urls(client, tok, job["id"], uris)
    assert r.status_code == 200, r.text
    assert set(r.json()["urls"]) == set(uris)


async def test_imagem_fora_do_gcs_e_422(admin, client, tmp_path):
    """Imagem importada de disco: o agente nunca a alcançaria, e o erro diz
    isso em vez de virar 503 de infraestrutura."""
    tok, jid = await _job_de_treino(admin, client, tmp_path)
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        # sample é imutável (trigger): o caso local entra como linha nova
        conn.execute(
            "INSERT INTO sample (project_id, sha256, uri, width, height)"
            " SELECT project_id, '\\x01', '/dados/local.jpg', 1, 1"
            "   FROM sample LIMIT 1")
        conn.execute(
            "INSERT INTO dataset_version_item (dataset_version_id, sample_id,"
            " split) SELECT (SELECT id FROM dataset_version LIMIT 1), id, 'train'"
            "   FROM sample WHERE uri = '/dados/local.jpg'")
    r = await _urls(client, tok, jid, ["/dados/local.jpg"])
    assert r.status_code == 422


async def test_imagem_de_bucket_fora_da_allowlist_nao_e_assinada(
        admin, client, tmp_path, assinaturas):
    """Imagem gravada antes da cerca do runner, apontando para outro bucket que
    a conta de serviço lê: estar na versão não basta para o agente recebê-la."""
    tok, jid = await _job_de_treino(admin, client, tmp_path)
    alheia = "gs://outro-cliente/frame.jpg"
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO sample (project_id, sha256, uri, width, height)"
            " SELECT project_id, '\\x02', %s, 1, 1 FROM sample LIMIT 1",
            (alheia,))
        conn.execute(
            "INSERT INTO dataset_version_item (dataset_version_id, sample_id,"
            " split) SELECT (SELECT id FROM dataset_version LIMIT 1), id, 'train'"
            "   FROM sample WHERE uri = %s", (alheia,))
    r = await _urls(client, tok, jid, [alheia])
    assert r.status_code == 403
    assert assinaturas == []


async def test_falha_de_assinatura_e_503_sem_texto_do_fornecedor(
        admin, client, tmp_path, monkeypatch):
    tok, jid = await _job_de_treino(admin, client, tmp_path)

    def explode(*a, **kw):
        raise RuntimeError("sa-interna@projeto.iam sem signBlob")

    monkeypatch.setattr(storage, "assinar_ou_falhar", explode)
    r = await _urls(client, tok, jid, _uris_da_versao())
    assert r.status_code == 503
    assert "sa-interna" not in r.text


# --------------------------------------------------------------------------- #
# Escrita
# --------------------------------------------------------------------------- #

async def test_upload_do_treino_vai_para_o_caminho_do_run(admin, client,
                                                          tmp_path, assinaturas):
    tok, jid = await _job_de_treino(admin, client, tmp_path)
    r = await client.post(f"/v1/agent/jobs/{jid}/upload",
                          json={"arquivo": "best.pt"}, headers=_h(tok))
    assert r.status_code == 200, r.text
    corpo = r.json()
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        run_id = conn.execute("SELECT payload->>'run_id' FROM job WHERE id = %s",
                              (jid,)).fetchone()[0]
    assert corpo["uri"] == (
        f"gs://{settings.models_bucket}/{settings.models_prefix}/"
        f"{PROJ['slug']}/detector/{run_id}/best.pt")
    assert corpo["headers"] == {"Content-Type": "application/octet-stream"}
    assert assinaturas == [("PUT", corpo["uri"])]


@pytest.mark.parametrize("nome", ["../best.pt", "a/b.pt", ".oculto", ""])
async def test_upload_nome_que_escapa_da_pasta_e_recusado(admin, client,
                                                          tmp_path, nome):
    tok, jid = await _job_de_treino(admin, client, tmp_path)
    r = await client.post(f"/v1/agent/jobs/{jid}/upload",
                          json={"arquivo": nome}, headers=_h(tok))
    assert r.status_code == 422


async def test_upload_em_job_de_prelabel_e_404(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel", json={
        "model_slug": "detector", "dataset_slug": DS["slug"]})
    tok = (await _device(admin, {"prelabel": True}, slug="pre"))["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    r = await client.post(f"/v1/agent/jobs/{job['id']}/upload",
                          json={"arquivo": "x.pt"}, headers=_h(tok))
    assert r.status_code == 404


async def test_sem_bucket_de_modelos_e_503(admin, client, tmp_path,
                                           monkeypatch):
    tok, jid = await _job_de_treino(admin, client, tmp_path)
    monkeypatch.setattr(settings, "models_bucket", "")
    r = await client.post(f"/v1/agent/jobs/{jid}/upload",
                          json={"arquivo": "best.pt"}, headers=_h(tok))
    assert r.status_code == 503
    assert "VISIONYARD_MODELS_BUCKET" in r.json()["detail"]


async def test_dois_exports_da_mesma_versao_nao_dividem_objeto(admin, client,
                                                               tmp_path):
    """fp16 e int8 dão o mesmo `best.engine`: no caminho antigo o segundo
    sobrescrevia o peso que o primeiro já tinha registrado."""
    await _produzir_modelo(admin, client, tmp_path)
    url = f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/export"
    await admin.post(url, json={"format": "engine", "precision": "fp16"})
    await admin.post(url, json={"format": "engine", "precision": "int8"})
    tok = (await _device(admin, {"infer": True}, slug="exp"))["token"]
    j1 = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    j2 = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    u1 = await _publicar(client, tok, j1["id"], "best.engine")
    u2 = await _publicar(client, tok, j2["id"], "best.engine")
    assert u1 != u2


# --------------------------------------------------------------------------- #
# Registro: só o que está onde a API mandou publicar
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("uri", [
    "gs://outro-cliente/peso.pt",
    "gs://modelos-de-teste/models/outro-projeto/detector/x/best.pt",
])
async def test_modelo_fora_do_destino_nao_registra(admin, client, tmp_path, uri):
    tok, jid = await _job_de_treino(admin, client, tmp_path)
    r = await client.post(f"/v1/agent/jobs/{jid}/model", json={
        "weights_uri": uri, "sha256": "ab" * 32, "metrics": {}},
        headers=_h(tok))
    assert r.status_code == 422


async def test_modelo_em_subpasta_do_destino_nao_registra(admin, client,
                                                          tmp_path):
    """O prefixo certo não basta: `<run>/../outro` ou `<run>/a/b` saem da
    folha que a API batizou."""
    tok, jid = await _job_de_treino(admin, client, tmp_path)
    base = (await _publicar(client, tok, jid, "best.pt"))[:-len("best.pt")]
    for uri in (base + "a/best.pt", base + "../x/best.pt"):
        r = await client.post(f"/v1/agent/jobs/{jid}/model", json={
            "weights_uri": uri, "sha256": "ab" * 32, "metrics": {}},
            headers=_h(tok))
        assert r.status_code == 422, uri


async def test_build_fora_do_destino_nao_registra(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/export",
        json={"format": "onnx"})
    tok = (await _device(admin, {"infer": True}, slug="exp"))["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    r = await client.post(f"/v1/agent/jobs/{job['id']}/build", json={
        "format": "onnx", "uri": "gs://qualquer/x.onnx", "sha256": "aa" * 32,
        "target": {}, "bench": {}}, headers=_h(tok))
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Deployment
# --------------------------------------------------------------------------- #

async def test_implantacao_pendente_vem_com_url(admin, client, tmp_path):
    from .test_deploy import _modelo_com_build
    await _modelo_com_build(admin, client, tmp_path)
    tok = (await _device(admin, {"infer": True}, slug="edge-prod"))["token"]
    await admin.post(f"/v1/projects/{PROJ['slug']}/models/detector/deploy",
                     json={"version": 1, "format": "onnx",
                           "device_slug": "edge-prod"})
    pend = (await client.get("/v1/agent/deployments/pending",
                             headers=_h(tok))).json()["deployments"]
    assert len(pend) == 1
    assert pend[0]["url"] == f"https://assinada/GET/{pend[0]['uri'][5:]}"


# --------------------------------------------------------------------------- #
# A porta
# --------------------------------------------------------------------------- #

class _Blob:
    def __init__(self, chamadas):
        self.chamadas = chamadas

    def generate_signed_url(self, **kw):
        self.chamadas.append(kw)
        return "https://assinada"


class _Cliente:
    def __init__(self, assina=True):
        self.chamadas: list[dict] = []
        self._credentials = type("C", (), {"signer_email": "x"})() if assina \
            else object()

    def bucket(self, nome):
        return type("B", (), {"blob": lambda _s, k: _Blob(self.chamadas)})()


def test_porta_estrita_falha_sem_credencial_que_assina(monkeypatch):
    """Ao contrário de `assinar()`, que devolve vazio para a galeria."""
    monkeypatch.undo()       # tira o falso do conftest: aqui é a real
    monkeypatch.setattr(storage, "cliente", lambda: _Cliente(assina=False))
    with pytest.raises(RuntimeError):
        storage.assinar_ou_falhar(["gs://b/a"], 5)


class _CredencialDeVm:
    """compute_engine.Credentials: sem chave e sem `signer_email`; o e-mail
    só se resolve no refresh."""

    def __init__(self):
        self.service_account_email = "default"
        self.token = None
        self.valid = False

    def refresh(self, request):
        self.service_account_email = "api@projeto.iam.gserviceaccount.com"
        self.token = "tok"
        self.valid = True


def test_porta_estrita_assina_pelo_iam_com_credencial_de_vm(monkeypatch):
    """Na nuvem a credencial é da VM: sem e-mail e token a biblioteca desiste
    com "you need a private key" — medido com google-cloud-storage 3.13."""
    monkeypatch.undo()
    c = _Cliente()
    c._credentials = _CredencialDeVm()
    monkeypatch.setattr(storage, "cliente", lambda: c)
    storage.assinar_ou_falhar(["gs://b/a.jpg"], 5)
    assert c.chamadas[0]["service_account_email"] == \
        "api@projeto.iam.gserviceaccount.com"
    assert c.chamadas[0]["access_token"] == "tok"


def test_galeria_tambem_assina_com_credencial_de_vm(monkeypatch):
    c = _Cliente()
    c._credentials = _CredencialDeVm()
    monkeypatch.setattr(storage, "cliente", lambda: c)
    assert list(storage.assinar(["gs://b/a.jpg"], 5, ["b"])) == ["gs://b/a.jpg"]
    assert c.chamadas[0]["access_token"] == "tok"


def test_porta_estrita_assina_put_com_tipo(monkeypatch):
    monkeypatch.undo()
    c = _Cliente()
    monkeypatch.setattr(storage, "cliente", lambda: c)
    storage.assinar_ou_falhar(["gs://b/a.pt"], 5, "PUT",
                              "application/octet-stream")
    assert c.chamadas[0]["method"] == "PUT"
    assert c.chamadas[0]["content_type"] == "application/octet-stream"
    with pytest.raises(ValueError):
        storage.assinar_ou_falhar(["/local/a.jpg"], 5)
