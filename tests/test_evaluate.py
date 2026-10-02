"""Avaliação: medir uma versão contra um benchmark e registrar a métrica."""
from __future__ import annotations

import pytest

from .test_datasets import DS, PROJ
from .test_models import _produzir_modelo
from .test_training import _device, _h, _publicar

pytestmark = pytest.mark.anyio


async def _claim_eval(admin, client) -> tuple[str, dict]:
    dev = await _device(admin, {"infer": True}, slug="eval-box")
    tok = dev["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    return tok, job


async def test_avaliar_registra_metrica_e_aparece(admin, client, tmp_path):
    # _produzir_modelo congela DS v1; usamos como benchmark
    await _produzir_modelo(admin, client, tmp_path)
    r = await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/evaluate",
        json={"dataset_slug": DS["slug"], "dataset_version": 1})
    assert r.status_code == 201, r.text

    tok, job = await _claim_eval(admin, client)
    assert job["type"] == "evaluate"
    assert job["payload"]["dataset_version_id"]

    r = await client.post(f"/v1/agent/jobs/{job['id']}/evaluation", json={
        "metrics": {"mAP50-95": 0.48, "mAP50": 0.71}}, headers=_h(tok))
    assert r.json()["evaluation_id"]

    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/models/detector")).json()
    v1 = next(v for v in d["versoes"] if v["version"] == 1)
    assert len(v1["avaliacoes"]) == 1
    a = v1["avaliacoes"][0]
    assert a["benchmark"] == DS["slug"] and a["benchmark_version"] == 1
    assert a["metrics"]["mAP50-95"] == 0.48


async def test_reavaliar_mostra_a_mais_recente(admin, client, tmp_path):
    """Duas medições do mesmo par: a comparação mostra a última, não duplica."""
    await _produzir_modelo(admin, client, tmp_path)
    url = f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/evaluate"
    dev = await _device(admin, {"infer": True}, slug="eval-box")
    tok = dev["token"]
    for mp in (0.40, 0.52):
        await admin.post(url, json={"dataset_slug": DS["slug"], "dataset_version": 1})
        job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
        await client.post(f"/v1/agent/jobs/{job['id']}/evaluation",
                          json={"metrics": {"mAP50-95": mp}}, headers=_h(tok))
    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/models/detector")).json()
    v1 = next(v for v in d["versoes"] if v["version"] == 1)
    assert len(v1["avaliacoes"]) == 1
    assert v1["avaliacoes"][0]["metrics"]["mAP50-95"] == 0.52


async def test_avaliar_benchmark_inexistente(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    r = await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/evaluate",
        json={"dataset_slug": DS["slug"], "dataset_version": 9})
    assert r.status_code == 404


async def test_avaliar_exige_engineer(admin, client, tmp_path):
    from .test_projects import novo_usuario
    await _produzir_modelo(admin, client, tmp_path)
    async with novo_usuario(admin, client, "rot@teste.com") as (rot, _):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "rot@teste.com", "role": "annotator"})
        r = await rot.post(
            f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/evaluate",
            json={"dataset_slug": DS["slug"], "dataset_version": 1})
        assert r.status_code == 403


async def _build_onnx(admin, client) -> None:
    """Registra um build onnx da detector v1 (para avaliar como formato)."""
    await admin.post(f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/export",
                     json={"format": "onnx"})
    dev = await _device(admin, {"infer": True}, slug="build-box")
    job = (await client.post("/v1/agent/claim", headers=_h(dev["token"]))).json()["job"]
    await client.post(f"/v1/agent/jobs/{job['id']}/build", json={
        "format": "onnx",
        "uri": await _publicar(client, dev["token"], job["id"], "best.onnx"),
        "sha256": "cc" * 32,
        "bytes": 1, "target": {"imgsz": 640}, "bench": {}}, headers=_h(dev["token"]))


async def test_compara_pt_e_onnx_como_linhas_distintas(admin, client, tmp_path):
    """pt e engine/onnx aparecem como avaliações separadas (a comparação)."""
    await _produzir_modelo(admin, client, tmp_path)
    await _build_onnx(admin, client)
    url = f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/evaluate"
    dev = await _device(admin, {"infer": True}, slug="eval-box")
    tok = dev["token"]
    for fmt, mp in (("pt", 0.50), ("onnx", 0.49)):
        r = await admin.post(url, json={"dataset_slug": DS["slug"],
                                        "dataset_version": 1, "format": fmt})
        assert r.status_code == 201, r.text
        job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
        # o agente reporta mAP + a decomposição de latência
        await client.post(f"/v1/agent/jobs/{job['id']}/evaluation", json={
            "metrics": {"mAP50-95": mp, "pre_ms": 1.2, "inf_ms": 8.6, "pos_ms": 0.9}},
            headers=_h(tok))

    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/models/detector")).json()
    v1 = next(v for v in d["versoes"] if v["version"] == 1)
    formatos = sorted(a["format"] for a in v1["avaliacoes"])
    assert formatos == ["onnx", "pt"], f"não separou por formato: {formatos}"
    # a métrica de latência foi guardada para o gráfico
    assert all(a["metrics"]["inf_ms"] == 8.6 for a in v1["avaliacoes"])


async def test_avaliar_engine_sem_build_recusa(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    r = await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/evaluate",
        json={"dataset_slug": DS["slug"], "dataset_version": 1, "format": "engine"})
    assert r.status_code == 404
    assert "compile antes" in r.json()["detail"]


async def test_avaliacao_le_o_manifesto_do_benchmark(admin, client, tmp_path):
    """O manifesto tem dois leitores, e a avaliação é o segundo.

    `agent/evaluate.py` pede `/manifest` e `/manifest/items` antes de medir: as
    caixas do benchmark são a verdade contra a qual o artefato é comparado.
    Quando a rota passou a ler o payload pelo contrato, passou a exigir o de
    treino — e toda avaliação virava 404 antes de baixar o benchmark.
    """
    await _produzir_modelo(admin, client, tmp_path)
    r = await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/evaluate",
        json={"dataset_slug": DS["slug"], "dataset_version": 1})
    assert r.status_code == 201, r.text
    tok, job = await _claim_eval(admin, client)
    assert job["type"] == "evaluate"

    meta = await client.get(f"/v1/agent/jobs/{job['id']}/manifest", headers=_h(tok))
    assert meta.status_code == 200, meta.text
    assert meta.json()["classes"] == ["classe_a", "classe_b"]
    itens = await client.get(f"/v1/agent/jobs/{job['id']}/manifest/items",
                             headers=_h(tok))
    assert itens.status_code == 200
