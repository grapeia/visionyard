"""Export: compilar uma versão para onnx/engine e registrar o build."""
from __future__ import annotations

import pytest

from .test_datasets import PROJ
from .test_models import _produzir_modelo
from .test_training import _device, _h, _publicar

pytestmark = pytest.mark.anyio


async def _claim_export(admin, client) -> tuple[str, dict]:
    dev = await _device(admin, {"infer": True}, slug="edge-export")
    tok = dev["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    return tok, job


async def test_export_enfileira_e_registra_build(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    r = await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/export",
        json={"format": "engine", "precision": "int8", "imgsz": 640, "dla": True})
    assert r.status_code == 201, r.text

    tok, job = await _claim_export(admin, client)
    assert job is not None and job["type"] == "export"
    assert job["payload"]["format"] == "engine"

    # o agente registra o build (a máquina define o device_class do alvo)
    r = await client.post(f"/v1/agent/jobs/{job['id']}/build", json={
        "format": "engine",
        "uri": await _publicar(client, tok, job["id"], "best.engine"),
        "sha256": "ef" * 32, "bytes": 4_000_000,
        "target": {"precision": "int8", "imgsz": 640, "dla": True},
        "bench": {"p50_ms": 8.6, "p95_ms": 10.2}}, headers=_h(tok))
    assert r.json()["ok"] is True
    assert r.json()["target"]["device_class"] == "gpu-server"  # do device

    # aparece no detalhe do modelo, na versão certa
    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/models/detector")).json()
    v1 = next(v for v in d["versoes"] if v["version"] == 1)
    assert len(v1["builds"]) == 1
    assert v1["builds"][0]["format"] == "engine"
    assert v1["builds"][0]["bench"]["p50_ms"] == 8.6
    assert v1["builds"][0]["target"]["precision"] == "int8"


async def test_export_engine_dynamic_por_padrao_e_estatico_no_dla(
        admin, client, tmp_path):
    """Engine compila com shapes dinâmicos por padrão — letterbox igual ao do
    .pt, detecções idênticas (medido em produção: 100% vs 96,7% estático).
    DLA só aceita shapes estáticos, então dla=True desliga o dynamic."""
    await _produzir_modelo(admin, client, tmp_path)
    r = await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/export",
        json={"format": "engine", "precision": "fp16"})
    assert r.status_code == 201, r.text
    tok, job = await _claim_export(admin, client)
    assert job["payload"]["dynamic"] is True

    r = await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/export",
        json={"format": "engine", "precision": "int8", "dla": True,
              "dynamic": True})
    assert r.status_code == 201, r.text
    job2 = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    assert job2["payload"]["dynamic"] is False

    # o alvo do build registra o dynamic (payload sem a chave = estático, que
    # é o default do contrato — job enfileirado antes dela continua na fila)
    from agent.export import _detectar_alvo
    from contracts import jobs
    base = {"model_version_id": "x", "weights_uri": "gs://b/w.pt",
            "model_slug": "detector", "project_slug": "p", "version": 1}
    din = _detectar_alvo(jobs.Export.de_payload({**base, "dynamic": True}))
    assert din["dynamic"] is True
    assert _detectar_alvo(jobs.Export.de_payload(base))["dynamic"] is False


async def test_build_mesmo_alvo_nao_duplica(admin, client, tmp_path):
    """model_build é imutável e único por (versão, formato, alvo)."""
    await _produzir_modelo(admin, client, tmp_path)
    await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/export",
        json={"format": "onnx"})
    tok, job = await _claim_export(admin, client)
    corpo = {"format": "onnx",
             "uri": await _publicar(client, tok, job["id"], "x.onnx"),
             "sha256": "aa" * 32,
             "bytes": 1, "target": {"imgsz": 640}, "bench": {}}
    r1 = await client.post(f"/v1/agent/jobs/{job['id']}/build", json=corpo,
                           headers=_h(tok))
    r2 = await client.post(f"/v1/agent/jobs/{job['id']}/build", json=corpo,
                           headers=_h(tok))
    assert r1.json().get("build_id")
    assert r2.json().get("ja_existia") is True

    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/models/detector")).json()
    v1 = next(v for v in d["versoes"] if v["version"] == 1)
    assert len(v1["builds"]) == 1


async def test_export_versao_inexistente(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    r = await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/9/export",
        json={"format": "onnx"})
    assert r.status_code == 404


async def test_export_exige_engineer(admin, client, tmp_path):
    from .test_projects import novo_usuario
    await _produzir_modelo(admin, client, tmp_path)
    async with novo_usuario(admin, client, "rot@teste.com") as (rot, _):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "rot@teste.com", "role": "annotator"})
        r = await rot.post(
            f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/export",
            json={"format": "onnx"})
        assert r.status_code == 403
