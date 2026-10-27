"""Registro de modelos: versões e apelidos (champion/challenger/baseline)."""
from __future__ import annotations

import pytest

from .test_datasets import PROJ
from .test_training import _criar_run, _device, _h, _publicar, _versao_congelada

pytestmark = pytest.mark.anyio


async def _produzir_modelo(admin, client, tmp_path) -> None:
    """Roda o protocolo do agente até registrar detector v1."""
    await _versao_congelada(admin, tmp_path)
    await _criar_run(admin)
    dev = await _device(admin, {"train": True})
    tok = dev["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    jid = job["id"]
    await client.post(f"/v1/agent/jobs/{jid}/heartbeat",
                      json={"progress": {}}, headers=_h(tok))
    await client.post(f"/v1/agent/jobs/{jid}/model", json={
        "weights_uri": await _publicar(client, tok, jid, "best.pt"),
        "sha256": "cd" * 32,
        "bytes": 5_000_000, "metrics": {"mAP50-95": 0.62}}, headers=_h(tok))
    await client.post(f"/v1/agent/jobs/{jid}/finish",
                      json={"status": "succeeded"}, headers=_h(tok))


async def test_lista_e_detalhe(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    modelos = (await admin.get(f"/v1/projects/{PROJ['slug']}/models")).json()
    assert len(modelos) == 1
    assert modelos[0]["slug"] == "detector"
    assert modelos[0]["versoes"] == 1 and modelos[0]["ultima"] == 1

    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/models/detector")).json()
    assert d["versoes"][0]["version"] == 1
    assert d["versoes"][0]["metrics"]["mAP50-95"] == 0.62
    # linhagem: a versão sabe de qual dataset/experimento veio
    assert d["versoes"][0]["experimento"] == "baseline"
    assert d["versoes"][0]["dataset_version"] == 1


async def test_viewer_le_os_parametros_de_treino_da_versao(admin, client, tmp_path):
    """O que treinou a versão chega à tela pela rota de leitura, que é `viewer`."""
    from .test_projects import novo_usuario
    await _produzir_modelo(admin, client, tmp_path)
    async with novo_usuario(admin, client, "ver@teste.com") as (ver, _):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "ver@teste.com", "role": "viewer"})
        r = await ver.get(f"/v1/projects/{PROJ['slug']}/models/detector")
    assert r.status_code == 200, r.text
    v = r.json()["versoes"][0]
    assert v["params"]["epochs"] == 3
    assert v["base_model"] == "yolo11n.pt"
    assert v["duration_s"] is not None


async def test_promover_para_champion(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    r = await admin.put(
        f"/v1/projects/{PROJ['slug']}/models/detector/aliases/champion",
        json={"version": 1})
    assert r.status_code == 200, r.text

    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/models/detector")).json()
    assert any(a["alias"] == "champion" and a["version"] == 1
               for a in d["aliases"])
    m = (await admin.get(f"/v1/projects/{PROJ['slug']}/models")).json()[0]
    assert m["aliases"] == {"champion": 1}


async def test_mover_alias_para_outra_versao(admin, client, tmp_path):
    """champion é um ponteiro: apontar de novo apenas move, não duplica."""
    await _produzir_modelo(admin, client, tmp_path)
    base = f"/v1/projects/{PROJ['slug']}/models/detector/aliases/champion"
    await admin.put(base, json={"version": 1})
    # aponta de novo para a mesma versão (idempotente na prática)
    r = await admin.put(base, json={"version": 1})
    assert r.status_code == 200
    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/models/detector")).json()
    assert len([a for a in d["aliases"] if a["alias"] == "champion"]) == 1


async def test_apelido_invalido_recusa(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    r = await admin.put(
        f"/v1/projects/{PROJ['slug']}/models/detector/aliases/producao",
        json={"version": 1})
    assert r.status_code == 422


async def test_promover_versao_inexistente(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    r = await admin.put(
        f"/v1/projects/{PROJ['slug']}/models/detector/aliases/champion",
        json={"version": 9})
    assert r.status_code == 404


async def test_remover_alias(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    base = f"/v1/projects/{PROJ['slug']}/models/detector/aliases/challenger"
    await admin.put(base, json={"version": 1})
    assert (await admin.delete(base)).status_code == 204
    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/models/detector")).json()
    assert d["aliases"] == []


async def test_annotator_nao_promove(admin, client, tmp_path):
    from .test_projects import novo_usuario
    await _produzir_modelo(admin, client, tmp_path)
    async with novo_usuario(admin, client, "rot@teste.com") as (rot, _):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "rot@teste.com", "role": "annotator"})
        r = await rot.put(
            f"/v1/projects/{PROJ['slug']}/models/detector/aliases/champion",
            json={"version": 1})
        assert r.status_code == 403


@pytest.fixture
def downloads(monkeypatch) -> list[tuple[str, str]]:
    """A URL de download assinada, sem GCS: registra (uri, nome do arquivo)."""
    from api import storage
    feitas: list[tuple[str, str]] = []

    def falsa(uri, minutos, nome):
        feitas.append((uri, nome))
        return f"https://assinada/download/{uri[5:]}"

    monkeypatch.setattr(storage, "assinar_download", falsa)
    return feitas


async def test_baixar_peso_redireciona_para_url_assinada(admin, client, tmp_path,
                                                         downloads):
    """O bucket é privado: o download é um redirect para uma URL assinada de
    vida curta, emitida a cada clique, com nome de arquivo legível."""
    await _produzir_modelo(admin, client, tmp_path)
    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/models/detector")).json()
    uri = d["versoes"][0]["weights_uri"]

    r = await admin.get(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/download",
        follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers["location"] == f"https://assinada/download/{uri[5:]}"
    assert downloads == [(uri, "detector-v1.pt")]

    r = await admin.get(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/9/download",
        follow_redirects=False)
    assert r.status_code == 404


async def test_baixar_peso_exige_engenheiro(admin, client, tmp_path, downloads):
    """Visualizador vê as métricas, não leva o peso."""
    from .test_projects import novo_usuario
    await _produzir_modelo(admin, client, tmp_path)
    async with novo_usuario(admin, client, "ver@teste.com") as (ver, _):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "ver@teste.com", "role": "viewer"})
        r = await ver.get(
            f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/download",
            follow_redirects=False)
        assert r.status_code == 403
    assert downloads == []
