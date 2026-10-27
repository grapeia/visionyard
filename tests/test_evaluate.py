"""Avaliação: medir uma versão contra um benchmark e registrar a métrica."""
from __future__ import annotations

import json
import uuid

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


async def test_avaliacao_guarda_o_ambiente_da_medicao(admin, client, tmp_path):
    """O tempo de inferência só se compara entre medições cuja máquina se
    conhece: o ambiente vai junto da avaliação, que é imutável."""
    await _produzir_modelo(admin, client, tmp_path)
    await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/evaluate",
        json={"dataset_slug": DS["slug"], "dataset_version": 1})
    tok, job = await _claim_eval(admin, client)
    r = await client.post(f"/v1/agent/jobs/{job['id']}/evaluation", json={
        "metrics": {"mAP50-95": 0.5},
        "environment": {"gpu": "RTX 4090", "driver": "550.54", "cuda": "12.4",
                        "runtime": "tensorrt", "trt": "10.0.1",
                        "agent_version": "0.3.0+abc1234",
                        # quem mediu é o servidor quem diz, não o agente
                        "device": "outra-maquina",
                        "segredo": "x" * 10}}, headers=_h(tok))
    assert r.status_code == 200, r.text

    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/models/detector")).json()
    amb = d["versoes"][0]["avaliacoes"][0]["environment"]
    assert amb["device"] == "eval-box"
    assert amb["gpu"] == "RTX 4090" and amb["runtime"] == "tensorrt"
    assert amb["agent_version"] == "0.3.0+abc1234"
    assert "segredo" not in amb


async def test_agente_antigo_cai_no_que_a_maquina_declarou(admin, client, tmp_path):
    """O agente 0.2 não manda ambiente: vale o que ele anunciou no registro."""
    await _produzir_modelo(admin, client, tmp_path)
    await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/evaluate",
        json={"dataset_slug": DS["slug"], "dataset_version": 1})
    tok, job = await _claim_eval(admin, client)
    await client.post("/v1/agent/register", json={
        "capabilities": {"gpu": "Orin", "cuda": "12.2"},
        "agent_version": "0.2.0"}, headers=_h(tok))
    await client.post(f"/v1/agent/jobs/{job['id']}/evaluation",
                      json={"metrics": {"mAP50-95": 0.5}}, headers=_h(tok))

    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/models/detector")).json()
    amb = d["versoes"][0]["avaliacoes"][0]["environment"]
    assert amb == {"device": "eval-box", "gpu": "Orin", "cuda": "12.2",
                   "agent_version": "0.2.0"}


# --------------------------------------------------------------------------- #
# Galeria: o que o modelo previu em cada imagem do benchmark
# --------------------------------------------------------------------------- #

async def _avaliacao_registrada(admin, client, tok: str | None = None):
    """Enfileira, reclama e registra uma avaliação de detector v1 no benchmark.
    Devolve (token, job_id, evaluation_id, md5s das imagens do benchmark)."""
    await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/evaluate",
        json={"dataset_slug": DS["slug"], "dataset_version": 1})
    if tok is None:
        tok = (await _device(admin, {"infer": True}, slug="eval-box"))["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    itens = await client.get(f"/v1/agent/jobs/{job['id']}/manifest/items",
                             headers=_h(tok))
    md5s = [json.loads(linha)["md5"] for linha in itens.text.splitlines()]
    r = await client.post(f"/v1/agent/jobs/{job['id']}/evaluation",
                          json={"metrics": {"mAP50-95": 0.5}}, headers=_h(tok))
    return tok, job["id"], r.json()["evaluation_id"], md5s


def _pred(md5: str, *, fp=0, fn=0, swapped=0) -> dict:
    return {"md5": md5, "gt": [[0, 0.5, 0.5, 0.2, 0.2]],
            "pred": [[0, 0.9, 0.5, 0.5, 0.2, 0.2]],
            "fp": fp, "fn": fn, "swapped": swapped}


async def test_galeria_mostra_as_imagens_que_erraram(admin, client, tmp_path):
    from .test_projects import novo_usuario
    await _produzir_modelo(admin, client, tmp_path)
    tok, jid, ev, md5s = await _avaliacao_registrada(admin, client)
    assert len(md5s) >= 2
    r = await client.post(f"/v1/agent/jobs/{jid}/evaluation/predictions", json={
        "predicoes": [_pred(md5s[0]), _pred(md5s[1], fn=1),
                      # imagem que não é do benchmark não grava em lugar nenhum
                      _pred("0" * 32, fp=3)]}, headers=_h(tok))
    assert r.status_code == 200, r.text
    assert r.json()["gravadas"] == 2

    url = f"/v1/projects/{PROJ['slug']}/models/detector/evaluations/{ev}/gallery"
    async with novo_usuario(admin, client, "ver@teste.com") as (ver, _):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "ver@teste.com", "role": "viewer"})
        g = (await ver.get(url, params={"filtro": "erros"})).json()
    assert g["total"] == 1
    assert g["itens"][0]["fn"] == 1
    assert g["itens"][0]["pred"] == [[0, 0.9, 0.5, 0.5, 0.2, 0.2]]
    assert g["classes"] == ["classe_a", "classe_b"]
    assert g["resumo"] == {"imagens": 2, "com_erro": 1, "fp": 0, "fn": 1,
                           "swapped": 0}
    # o detalhe do modelo avisa que esta avaliação tem galeria
    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/models/detector")).json()
    a = d["versoes"][0]["avaliacoes"][0]
    assert a["id"] == ev and a["tem_galeria"] is True


async def test_galeria_compara_duas_avaliacoes_imagem_a_imagem(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    tok, jid, ev_a, md5s = await _avaliacao_registrada(admin, client)
    await client.post(f"/v1/agent/jobs/{jid}/evaluation/predictions", json={
        "predicoes": [_pred(md5s[0]), _pred(md5s[1], fn=1)]}, headers=_h(tok))
    # a segunda erra a imagem que a primeira acertou, e vice-versa
    _, jid_b, ev_b, _ = await _avaliacao_registrada(admin, client, tok)
    await client.post(f"/v1/agent/jobs/{jid_b}/evaluation/predictions", json={
        "predicoes": [_pred(md5s[0], swapped=1), _pred(md5s[1], fn=1)]},
        headers=_h(tok))

    url = f"/v1/projects/{PROJ['slug']}/models/detector/evaluations/{ev_a}/gallery"
    g = (await admin.get(url, params={"filtro": "diferentes",
                                      "comparar": ev_b})).json()
    assert g["total"] == 1
    it = g["itens"][0]
    assert (it["swapped"], it["outra"]["swapped"]) == (0, 1)


async def test_galeria_recusa_comparar_fora_do_benchmark(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    _, _, ev, _ = await _avaliacao_registrada(admin, client)
    url = f"/v1/projects/{PROJ['slug']}/models/detector/evaluations/{ev}/gallery"
    r = await admin.get(url, params={"comparar": str(uuid.uuid4())})
    assert r.status_code == 404
    r = await admin.get(url, params={"filtro": "diferentes"})
    assert r.status_code == 422


async def test_predicoes_antes_da_avaliacao_sao_recusadas(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/evaluate",
        json={"dataset_slug": DS["slug"], "dataset_version": 1})
    tok, job = await _claim_eval(admin, client)
    r = await client.post(f"/v1/agent/jobs/{job['id']}/evaluation/predictions",
                          json={"predicoes": [_pred("0" * 32)]}, headers=_h(tok))
    assert r.status_code == 409


async def test_predicao_com_nan_e_recusada_com_422(admin, client, tmp_path):
    """O `jsonb` não aceita NaN: sem recusar na entrada, o lote virava 500."""
    await _produzir_modelo(admin, client, tmp_path)
    tok, jid, _, md5s = await _avaliacao_registrada(admin, client)
    for ruim in (float("nan"), float("inf")):
        p = _pred(md5s[0])
        p["pred"] = [[0, 0.9, 0.5, ruim, 0.2, 0.2]]
        # o httpx não serializa NaN; o json da stdlib sim, como `NaN` literal
        r = await client.post(f"/v1/agent/jobs/{jid}/evaluation/predictions",
                              content=json.dumps({"predicoes": [p]}),
                              headers={**_h(tok), "Content-Type": "application/json"})
        assert r.status_code == 422, r.text
    p = _pred(md5s[0])
    p["gt"] = [[0, 0.5]]                 # linha de gabarito sem caixa
    r = await client.post(f"/v1/agent/jobs/{jid}/evaluation/predictions",
                          json={"predicoes": [p]}, headers=_h(tok))
    assert r.status_code == 422, r.text
