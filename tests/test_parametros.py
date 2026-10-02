"""Parâmetros da instalação editáveis na tela.

O que importa conferir: o ambiente vence a tela (e trava o campo), o valor
gravado chega a quem o usa sem restart, e o que entra é validado — o prefixo
dos modelos, em especial, é a base contra a qual a API compara o que o agente
registra.
"""
from __future__ import annotations

import psycopg
import pytest

from api import storage
from api.services import parameters

from .conftest import TEST_DSN
from .test_datasets import PROJ
from .test_projects import novo_usuario
from .test_training import _criar_run, _device, _h, _versao_congelada

pytestmark = pytest.mark.anyio


@pytest.fixture
def destravado(monkeypatch):
    """Os testes fixam buckets e bucket de modelos no ambiente (conftest);
    aqui a tela passa a mandar em tudo."""
    monkeypatch.setattr(parameters, "travados", lambda: set())


def _por_chave(lista: list[dict]) -> dict[str, dict]:
    return {p["chave"]: p for p in lista}


async def test_so_superusuario(admin, client):
    async with novo_usuario(admin, client, "eng@teste.com") as (eng, _):
        assert (await eng.get("/v1/parameters")).status_code == 403
        r = await eng.put("/v1/parameters/models_prefix", json={"valor": "x"})
        assert r.status_code == 403
        assert (await eng.get("/v1/parameters/diagnostico")).status_code == 403


async def test_lista_diz_de_onde_vem_cada_valor(admin):
    ps = _por_chave((await admin.get("/v1/parameters")).json())
    assert set(ps) == {"buckets", "models_bucket", "models_prefix",
                       "agent_url_minutes"}
    # o conftest define estes dois no ambiente
    assert ps["models_bucket"]["origem"] == "ambiente"
    assert ps["models_bucket"]["valor"] == "modelos-de-teste"
    assert ps["models_bucket"]["variavel"] == "VISIONYARD_MODELS_BUCKET"
    assert ps["models_prefix"]["origem"] == "padrão"
    assert ps["models_prefix"]["valor"] == "models"


async def test_ambiente_trava_o_campo(admin):
    r = await admin.put("/v1/parameters/models_bucket",
                        json={"valor": "outro-bucket"})
    assert r.status_code == 409
    assert "VISIONYARD_MODELS_BUCKET" in r.json()["detail"]
    r = await admin.delete("/v1/parameters/models_bucket")
    assert r.status_code == 409


async def test_ambiente_vence_o_que_a_tela_gravou(admin, monkeypatch):
    """A variável que aparece depois de o valor ter sido gravado na tela
    passa a valer — o deploy declarativo é a última palavra."""
    monkeypatch.setattr(parameters, "travados", lambda: set())
    await admin.put("/v1/parameters/models_bucket",
                    json={"valor": "da-tela"})
    assert await parameters.valor("models_bucket") == "da-tela"

    monkeypatch.setattr(parameters, "travados", lambda: {"models_bucket"})
    parameters.esquecer()
    assert await parameters.valor("models_bucket") == "modelos-de-teste"
    ps = _por_chave((await admin.get("/v1/parameters")).json())
    assert ps["models_bucket"]["origem"] == "ambiente"


async def test_grava_audita_e_restaura(admin):
    r = await admin.put("/v1/parameters/agent_url_minutes", json={"valor": 30})
    assert r.status_code == 200, r.text
    assert r.json()["origem"] == "tela" and r.json()["valor"] == 30
    assert r.json()["alterado_por"] == "Admin de Teste"
    assert await parameters.valor("agent_url_minutes") == 30

    r = await admin.delete("/v1/parameters/agent_url_minutes")
    assert r.json()["origem"] == "padrão" and r.json()["valor"] == 60
    assert await parameters.valor("agent_url_minutes") == 60

    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        trilha = conn.execute(
            "SELECT action, before, after FROM audit_log"
            " WHERE entity_type = 'installation_setting' ORDER BY at, id"
        ).fetchall()
    assert [t[0] for t in trilha] == ["parameter.set", "parameter.reset"]
    assert trilha[0][2] == {"chave": "agent_url_minutes", "valor": 30}
    assert trilha[1][1] == {"chave": "agent_url_minutes", "valor": 30}


async def test_segunda_gravacao_audita_o_valor_substituido(admin):
    await admin.put("/v1/parameters/agent_url_minutes", json={"valor": 30})
    await admin.put("/v1/parameters/agent_url_minutes", json={"valor": 90})
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        antes = conn.execute(
            "SELECT before FROM audit_log WHERE action = 'parameter.set'"
            " ORDER BY at DESC, id DESC LIMIT 1").fetchone()[0]
    assert antes == {"chave": "agent_url_minutes", "valor": 30}


@pytest.mark.parametrize(("chave", "valor"), [
    ("agent_url_minutes", True),          # bool é int em Python
    ("agent_url_minutes", 0),
    ("agent_url_minutes", "30"),
    ("models_prefix", "a/../b"),
    ("models_prefix", "a//b"),
    ("models_prefix", ""),
    ("models_bucket", "Bucket_Com_Maiuscula"),
    ("models_bucket", "a"),
    ("buckets", "nao-e-lista"),
    ("buckets", ["ok-bucket", "x y"]),
])
async def test_valor_invalido_e_422(admin, destravado, chave, valor):
    r = await admin.put(f"/v1/parameters/{chave}", json={"valor": valor})
    assert r.status_code == 422, (chave, valor)
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM installation_setting"
                            ).fetchone()[0] == 0


async def test_normaliza_o_que_entra(admin, destravado):
    r = await admin.put("/v1/parameters/models_prefix",
                        json={"valor": " /modelos/v2/ "})
    assert r.json()["valor"] == "modelos/v2"
    r = await admin.put("/v1/parameters/buckets",
                        json={"valor": ["gs://imagens-a/", "imagens-a",
                                        "imagens-b"]})
    assert r.json()["valor"] == ["imagens-a", "imagens-b"]


async def test_desconhecido_e_404(admin):
    r = await admin.put("/v1/parameters/database_url", json={"valor": "x"})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# O valor chega a quem usa, sem restart
# --------------------------------------------------------------------------- #

async def test_prefixo_e_bucket_novos_valem_para_o_agente(admin, client,
                                                          tmp_path,
                                                          destravado):
    await admin.put("/v1/parameters/models_bucket",
                    json={"valor": "bucket-da-tela"})
    await admin.put("/v1/parameters/models_prefix",
                    json={"valor": "modelos/v2"})
    await _versao_congelada(admin, tmp_path)
    await _criar_run(admin)
    tok = (await _device(admin, {"train": True}))["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    r = await client.post(f"/v1/agent/jobs/{job['id']}/upload",
                          json={"arquivo": "best.pt"}, headers=_h(tok))
    assert r.json()["uri"].startswith(
        f"gs://bucket-da-tela/modelos/v2/{PROJ['slug']}/")
    # e o registro confere contra a MESMA base
    r = await client.post(f"/v1/agent/jobs/{job['id']}/model", json={
        "weights_uri": r.json()["uri"], "sha256": "ab" * 32, "metrics": {}},
        headers=_h(tok))
    assert r.status_code == 200, r.text


async def test_prefixo_mudado_entre_upload_e_registro_nao_perde_o_treino(
        admin, client, tmp_path):
    """O peso subiu no caminho que a API deu; alguém editou o prefixo antes
    do registro (ou o registro caiu num worker com cache diferente). Conferir
    contra o caminho recalculado dava 422 e jogava fora horas de treino."""
    await _versao_congelada(admin, tmp_path)
    await _criar_run(admin)
    tok = (await _device(admin, {"train": True}))["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    r = await client.post(f"/v1/agent/jobs/{job['id']}/upload",
                          json={"arquivo": "best.pt"}, headers=_h(tok))
    peso = r.json()["uri"]

    await admin.put("/v1/parameters/models_prefix", json={"valor": "outro"})
    r = await client.post(f"/v1/agent/jobs/{job['id']}/model", json={
        "weights_uri": peso, "sha256": "ab" * 32, "metrics": {}},
        headers=_h(tok))
    assert r.status_code == 200, r.text


async def test_agente_antigo_sem_upload_ainda_registra(admin, client, tmp_path):
    """O agente 0.1 publica sozinho e nunca chama `/upload`: o caminho
    recalculado continua valendo para ele."""
    await _versao_congelada(admin, tmp_path)
    await _criar_run(admin)
    tok = (await _device(admin, {"train": True}))["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        run_id = conn.execute("SELECT payload->>'run_id' FROM job WHERE id = %s",
                              (job["id"],)).fetchone()[0]
    uri = (f"gs://modelos-de-teste/models/{PROJ['slug']}/detector/{run_id}"
           f"/best.pt")
    r = await client.post(f"/v1/agent/jobs/{job['id']}/model", json={
        "weights_uri": uri, "sha256": "ab" * 32, "metrics": {}},
        headers=_h(tok))
    assert r.status_code == 200, r.text


async def test_destino_entregue_a_outro_job_nao_serve(admin, client, tmp_path):
    """`job_upload` é por job: o destino de um treino não vira passe para o
    registro de outro."""
    await _versao_congelada(admin, tmp_path)
    await _criar_run(admin)
    await _criar_run(admin)
    tok = (await _device(admin, {"train": True}))["token"]
    j1 = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    j2 = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    r = await client.post(f"/v1/agent/jobs/{j1['id']}/upload",
                          json={"arquivo": "best.pt"}, headers=_h(tok))
    r = await client.post(f"/v1/agent/jobs/{j2['id']}/model", json={
        "weights_uri": r.json()["uri"], "sha256": "ab" * 32, "metrics": {}},
        headers=_h(tok))
    assert r.status_code == 422


async def test_buckets_da_tela_viram_a_cerca_da_navegacao(admin, destravado,
                                                          monkeypatch):
    await admin.post("/v1/projects", json=PROJ)
    await admin.put("/v1/parameters/buckets", json={"valor": ["so-este"]})
    r = await admin.get(f"/v1/projects/{PROJ['slug']}/buckets")
    assert r.json() == [{"nome": "so-este"}]

    from api.services import buckets
    monkeypatch.setattr(buckets, "_navegar", lambda b, p: {
        "bucket": b, "prefixo": p, "pai": None, "pastas": [], "objetos": [],
        "tem_mais": False, "imagens": 0, "assina": False})
    base = f"/v1/projects/{PROJ['slug']}/buckets/browse"
    r = await admin.get(base, params={"bucket": "so-este"})
    assert r.status_code == 200, r.text      # a rota existe e navega o da lista
    r = await admin.get(base, params={"bucket": "bucket-de-teste"})
    assert r.status_code == 404, "bucket que saiu da lista seguiu navegável"


async def test_cache_de_outro_processo_vence_no_prazo(admin, destravado,
                                                      monkeypatch):
    """A API e o worker são processos diferentes: o que um grava o outro só
    vê quando o cache dele vence."""
    assert await parameters.valor("agent_url_minutes") == 60
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("INSERT INTO installation_setting (key, value)"
                     " VALUES ('agent_url_minutes', '15')")
    assert await parameters.valor("agent_url_minutes") == 60   # cache
    monkeypatch.setattr(parameters, "CACHE_S", 0.0)
    assert await parameters.valor("agent_url_minutes") == 15


async def test_validade_da_tela_vale_para_a_assinatura(admin, client, tmp_path,
                                                       monkeypatch):
    await admin.put("/v1/parameters/agent_url_minutes", json={"valor": 15})
    vistos = []

    def falsa(uris, minutos, metodo="GET", content_type=None):
        vistos.append(minutos)
        return dict.fromkeys(uris, "https://x")

    monkeypatch.setattr(storage, "assinar_ou_falhar", falsa)
    await _versao_congelada(admin, tmp_path)
    await _criar_run(admin)
    tok = (await _device(admin, {"train": True}))["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    await client.post(f"/v1/agent/jobs/{job['id']}/upload",
                      json={"arquivo": "best.pt"}, headers=_h(tok))
    assert vistos == [15]


# --------------------------------------------------------------------------- #
# Diagnóstico
# --------------------------------------------------------------------------- #

class _Blob:
    def __init__(self, nega):
        self.nega = nega

    def generate_signed_url(self, **kw):
        if self.nega:
            raise RuntimeError("403 Permission iam.serviceAccounts.signBlob")
        return "https://assinada"


class _Bucket:
    def __init__(self, nome, nega_assinatura=False):
        self.nome = nome
        self.nega_assinatura = nega_assinatura

    def blob(self, chave):
        return _Blob(self.nega_assinatura)

    def test_iam_permissions(self, pedidas):
        if self.nome == "sumido":
            raise type("NotFound", (Exception,), {})("404 GET https://...")
        if self.nome == "quebrado":
            raise RuntimeError("falha qualquer")
        if self.nome == "so-leitura":
            return [p for p in pedidas if p != "storage.objects.create"]
        return list(pedidas)


class _Cliente:
    def __init__(self, nega_assinatura=False):
        self._credentials = type("C", (), {"signer_email": "api@p.iam"})()
        self.nega_assinatura = nega_assinatura

    def bucket(self, nome):
        return _Bucket(nome, self.nega_assinatura)


def test_diagnostico_diz_o_que_falta_em_cada_bucket(monkeypatch):
    monkeypatch.setattr(storage, "cliente", lambda: _Cliente())
    d = storage.diagnosticar(["imagens", "sumido", "quebrado"], "so-leitura")
    assert d["conta"] == "api@p.iam" and d["assina"] is True
    assert d["origem"] == "chave de conta de serviço"
    por = {b["bucket"]: b for b in d["buckets"]}
    assert por["imagens"]["faltando"] == [] and por["imagens"]["erro"] is None
    assert por["sumido"]["erro"] == "bucket não existe — confira o nome"
    assert por["quebrado"]["erro"] == "RuntimeError: falha qualquer"
    assert por["so-leitura"]["papel"] == "modelos"
    assert por["so-leitura"]["faltando"] == ["storage.objects.create"]


def test_diagnostico_sem_signblob_nao_diz_que_assina(monkeypatch):
    """Token válido e acesso aos buckets, mas sem `signBlob`: olhar a
    credencial dizia "assina", e o agente levava 503 na primeira URL."""
    monkeypatch.setattr(storage, "cliente",
                        lambda: _Cliente(nega_assinatura=True))
    d = storage.diagnosticar(["imagens"], "modelos")
    assert d["assina"] is False
    assert "signBlob" in d["erro"]


def test_diagnostico_sem_credencial_nao_quebra(monkeypatch):
    def explode():
        raise RuntimeError("sem credencial")

    monkeypatch.setattr(storage, "cliente", explode)
    d = storage.diagnosticar(["imagens"], "")
    assert d["erro"].startswith("RuntimeError") and d["buckets"] == []


async def test_rota_de_diagnostico_usa_os_buckets_que_valem(admin, destravado,
                                                            monkeypatch):
    await admin.put("/v1/parameters/buckets", json={"valor": ["da-tela"]})
    pedidos = []

    def falso(buckets, modelos):
        pedidos.append((buckets, modelos))
        return {"conta": "x", "origem": "y", "assina": True, "erro": None,
                "buckets": []}

    monkeypatch.setattr(storage, "diagnosticar", falso)
    r = await admin.get("/v1/parameters/diagnostico")
    assert r.status_code == 200, r.text
    assert pedidos == [(["da-tela"], "modelos-de-teste")]
