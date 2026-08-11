"""Deployment: implantar um build num dispositivo e ativar (device-scoped)."""
from __future__ import annotations

import pytest

from .test_datasets import PROJ
from .test_models import _produzir_modelo
from .test_training import _device, _h

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
        "format": "onnx", "uri": "gs://b/MODELOS-VISIONYARD/v1/builds/best.onnx",
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
        "format": "engine", "uri": "gs://b/x.engine", "sha256": "bb" * 32,
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
