"""Deployment: implantar um build num dispositivo e ativar (device-scoped)."""
from __future__ import annotations

import psycopg
import pytest

from .conftest import TEST_DSN
from .test_datasets import PROJ
from .test_models import _produzir_modelo
from .test_training import _device, _h, _publicar

pytestmark = pytest.mark.anyio


async def _modelo_com_build(admin, client, tmp_path) -> None:
    """Model detector v1 + um build onnx registrado."""
    await _produzir_modelo(admin, client, tmp_path)
    await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/export",
        json={"format": "onnx"})
    dev = await _device(admin, {"infer": True}, slug="build-box")
    job = (await client.post("/v1/agent/claim", headers=_h(dev["token"]))).json()["job"]
    await client.post(f"/v1/agent/jobs/{job['id']}/build", json={
        "format": "onnx",
        "uri": await _publicar(client, dev["token"], job["id"], "best.onnx"),
        "sha256": "aa" * 32, "bytes": 4_000_000,
        "target": {"imgsz": 640}, "bench": {}}, headers=_h(dev["token"]))


async def test_deploy_pendente_ativa_e_supersede(admin, client, tmp_path):
    await _modelo_com_build(admin, client, tmp_path)
    prod = await _device(admin, {"infer": True}, slug="edge-prod")
    tok = prod["token"]

    r = await admin.post(f"/v1/projects/{PROJ['slug']}/models/detector/deploy",
                         json={"version": 1, "format": "onnx",
                               "device_slug": "edge-prod"})
    assert r.status_code == 201, r.text
    dep1 = r.json()["deployment_id"]

    # a máquina vê o deployment pendente com o URI do build
    pend = (await client.get("/v1/agent/deployments/pending",
                             headers=_h(tok))).json()["deployments"]
    assert len(pend) == 1 and pend[0]["id"] == dep1
    assert pend[0]["uri"].endswith("best.onnx")

    # ativa
    await client.post(f"/v1/agent/deployments/{dep1}/state",
                      json={"state": "active"}, headers=_h(tok))
    deps = (await admin.get(f"/v1/projects/{PROJ['slug']}/deployments")).json()
    ativo = [d for d in deps if d["state"] == "active"]
    assert len(ativo) == 1 and ativo[0]["device"] == "edge-prod"

    # segunda implantação no MESMO device supersede a primeira ao ativar
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/models/detector/deploy",
                         json={"version": 1, "format": "onnx",
                               "device_slug": "edge-prod"})
    dep2 = r.json()["deployment_id"]
    await client.post(f"/v1/agent/deployments/{dep2}/state",
                      json={"state": "active"}, headers=_h(tok))
    deps = (await admin.get(f"/v1/projects/{PROJ['slug']}/deployments")).json()
    ativos = [d for d in deps if d["state"] == "active"]
    assert len(ativos) == 1 and ativos[0]["id"] == dep2, "duas ativas no mesmo device"


def _estado(dep_id: str) -> str:
    """Lido do banco: `listar_deployments` esconde 'superseded'."""
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        row = conn.execute("SELECT state FROM deployment WHERE id = %s",
                           (dep_id,)).fetchone()
    return row[0]


async def _duas_implantacoes(admin, client, tmp_path) -> tuple[str, str, str]:
    """(token do device, dep1 já superseded, dep2 ativo)."""
    await _modelo_com_build(admin, client, tmp_path)
    prod = await _device(admin, {"infer": True}, slug="edge-prod")
    tok = prod["token"]
    url = f"/v1/projects/{PROJ['slug']}/models/detector/deploy"
    corpo = {"version": 1, "format": "onnx", "device_slug": "edge-prod"}

    dep1 = (await admin.post(url, json=corpo)).json()["deployment_id"]
    await client.post(f"/v1/agent/deployments/{dep1}/state",
                      json={"state": "active"}, headers=_h(tok))
    dep2 = (await admin.post(url, json=corpo)).json()["deployment_id"]
    await client.post(f"/v1/agent/deployments/{dep2}/state",
                      json={"state": "active"}, headers=_h(tok))
    return tok, dep1, dep2


async def test_relato_atrasado_nao_ressuscita_deployment(admin, client, tmp_path):
    """`active` reenviado depois de a implantação ter sido superseded não pode
    reativá-la: a tela passaria a mostrar um build que a máquina não está
    rodando. O agente reenvia porque o cliente repete em erro de rede."""
    tok, dep1, dep2 = await _duas_implantacoes(admin, client, tmp_path)

    r = await client.post(f"/v1/agent/deployments/{dep1}/state",
                          json={"state": "active"}, headers=_h(tok))
    assert r.status_code == 409, r.text

    deps = (await admin.get(f"/v1/projects/{PROJ['slug']}/deployments")).json()
    ativos = [d for d in deps if d["state"] == "active"]
    assert len(ativos) == 1 and ativos[0]["id"] == dep2
    assert _estado(dep1) == "superseded"


async def test_relato_de_falha_atrasado_nao_derruba_o_que_esta_valendo(
        admin, client, tmp_path):
    """Depois de o `active` estourar o timeout, o agente relata `failed` — mas o
    servidor já tinha comitado a ativação. O relato atrasado não pode marcar
    como falha a implantação que está no ar."""
    tok, _, dep2 = await _duas_implantacoes(admin, client, tmp_path)

    r = await client.post(f"/v1/agent/deployments/{dep2}/state",
                          json={"state": "failed", "error": "timeout"},
                          headers=_h(tok))
    assert r.status_code == 409, r.text
    deps = (await admin.get(f"/v1/projects/{PROJ['slug']}/deployments")).json()
    assert next(d for d in deps if d["id"] == dep2)["state"] == "active"


async def test_relato_repetido_do_mesmo_estado_e_idempotente(admin, client,
                                                             tmp_path):
    """O cliente do agente repete POST em erro de rede: reenviar o MESMO estado
    já registrado é sucesso, não conflito."""
    tok, _, dep2 = await _duas_implantacoes(admin, client, tmp_path)

    r = await client.post(f"/v1/agent/deployments/{dep2}/state",
                          json={"state": "active"}, headers=_h(tok))
    assert r.status_code == 200, r.text
    deps = (await admin.get(f"/v1/projects/{PROJ['slug']}/deployments")).json()
    ativos = [d for d in deps if d["state"] == "active"]
    assert len(ativos) == 1 and ativos[0]["id"] == dep2


async def test_ativacoes_simultaneas_no_mesmo_device_nao_colidem(admin, client,
                                                                 tmp_path):
    """Duas implantações pendentes na MESMA máquina, ativadas ao mesmo tempo.

    O `FOR UPDATE` travava só a linha relatada, então as duas transações
    passavam pela checagem de 'pending', nenhuma enxergava o `active` não
    comitado da outra e a segunda batia no índice único
    `deployment_active_idx` — 500 para o agente, e o deployment ficava preso em
    'pending' na tela embora a máquina já estivesse rodando o build.
    """
    import asyncio
    await _modelo_com_build(admin, client, tmp_path)
    prod = await _device(admin, {"infer": True}, slug="edge-prod")
    tok = prod["token"]
    url = f"/v1/projects/{PROJ['slug']}/models/detector/deploy"
    corpo = {"version": 1, "format": "onnx", "device_slug": "edge-prod"}
    dep1 = (await admin.post(url, json=corpo)).json()["deployment_id"]
    dep2 = (await admin.post(url, json=corpo)).json()["deployment_id"]

    respostas = await asyncio.gather(*(
        client.post(f"/v1/agent/deployments/{d}/state",
                    json={"state": "active"}, headers=_h(tok))
        for d in (dep1, dep2)), return_exceptions=True)
    for r in respostas:
        assert not isinstance(r, Exception), r
        assert r.status_code < 500, r.text

    deps = (await admin.get(f"/v1/projects/{PROJ['slug']}/deployments")).json()
    ativos = [d for d in deps if d["state"] == "active"]
    assert len(ativos) == 1, f"esperava uma ativa, veio {len(ativos)}"
    assert {_estado(dep1), _estado(dep2)} == {"active", "superseded"}


async def test_deploy_sem_build_recusa(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)   # sem build
    await _device(admin, {"infer": True}, slug="edge-prod")
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/models/detector/deploy",
                         json={"version": 1, "format": "engine",
                               "device_slug": "edge-prod"})
    assert r.status_code == 404
    assert "compile antes" in r.json()["detail"]


async def test_pending_so_da_maquina(admin, client, tmp_path):
    """Uma máquina não vê deployment de outra."""
    await _modelo_com_build(admin, client, tmp_path)
    await _device(admin, {"infer": True}, slug="edge-prod")
    outra = await _device(admin, {"infer": True}, slug="outra-box")
    await admin.post(f"/v1/projects/{PROJ['slug']}/models/detector/deploy",
                     json={"version": 1, "format": "onnx",
                           "device_slug": "edge-prod"})
    pend = (await client.get("/v1/agent/deployments/pending",
                             headers=_h(outra["token"]))).json()["deployments"]
    assert pend == []


async def test_deploy_falha_registrada(admin, client, tmp_path):
    await _modelo_com_build(admin, client, tmp_path)
    prod = await _device(admin, {"infer": True}, slug="edge-prod")
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/models/detector/deploy",
                         json={"version": 1, "format": "onnx",
                               "device_slug": "edge-prod"})
    dep = r.json()["deployment_id"]
    await client.post(f"/v1/agent/deployments/{dep}/state",
                      json={"state": "failed", "error": "sem espaço em disco"},
                      headers=_h(prod["token"]))
    deps = (await admin.get(f"/v1/projects/{PROJ['slug']}/deployments")).json()
    d = next(x for x in deps if x["id"] == dep)
    assert d["state"] == "failed" and "disco" in d["error"]


async def test_deploy_exige_engineer(admin, client, tmp_path):
    from .test_projects import novo_usuario
    await _modelo_com_build(admin, client, tmp_path)
    await _device(admin, {"infer": True}, slug="edge-prod")
    async with novo_usuario(admin, client, "rot@teste.com") as (rot, _):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "rot@teste.com", "role": "annotator"})
        r = await rot.post(f"/v1/projects/{PROJ['slug']}/models/detector/deploy",
                           json={"version": 1, "format": "onnx",
                                 "device_slug": "edge-prod"})
        assert r.status_code == 403


async def test_deploy_engine_classe_incompativel_recusa(admin, client, tmp_path):
    """Engine é específico da classe: implantar em classe diferente é recusado."""
    from .test_datasets import PROJ as P
    await _produzir_modelo(admin, client, tmp_path)
    await admin.post(f"/v1/projects/{P['slug']}/models/detector/versions/1/export",
                     json={"format": "engine", "precision": "int8"})
    # o build sai com o device_class da máquina que compilou: gpu-server
    builder = await _device(admin, {"infer": True}, slug="build-box")
    r = await client.post("/v1/agent/claim", headers=_h(builder["token"]))
    job = r.json()["job"]
    await client.post(f"/v1/agent/jobs/{job['id']}/build", json={
        "format": "engine",
        "uri": await _publicar(client, builder["token"], job["id"], "x.engine"),
        "sha256": "bb" * 32,
        "bytes": 1, "target": {"precision": "int8"}, "bench": {}},
        headers=_h(builder["token"]))
    # máquina-alvo de OUTRA classe
    await admin.post("/v1/devices", json={
        "slug": "edge-prod", "name": "dispositivo", "device_class": "edge-device",
        "capabilities": {"infer": True}})
    r = await admin.post(f"/v1/projects/{P['slug']}/models/detector/deploy",
                         json={"version": 1, "format": "engine",
                               "device_slug": "edge-prod"})
    assert r.status_code == 409
    assert "gpu-server" in r.json()["detail"]
