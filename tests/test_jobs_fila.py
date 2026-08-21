"""Fila global de jobs: a visão operacional (aba Máquinas) do que espera
máquina, do que roda onde, e o cancelamento direto da fila."""
from __future__ import annotations

import pytest

from .test_datasets import PROJ
from .test_models import _produzir_modelo
from .test_projects import novo_usuario
from .test_training import _device, _h

pytestmark = pytest.mark.anyio


async def _export_job(admin) -> str:
    r = await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/export",
        json={"format": "engine", "precision": "fp16"})
    assert r.status_code == 201, r.text
    return r.json()["job_id"]


async def test_fila_lista_jobs_com_projeto_e_maquina(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    job_id = await _export_job(admin)

    r = await admin.get("/v1/jobs")
    assert r.status_code == 200, r.text
    fila = r.json()
    meu = next(j for j in fila if j["id"] == job_id)
    assert meu["type"] == "export"
    assert meu["status"] == "queued"
    assert meu["project"] == PROJ["slug"]
    assert meu["device"] is None  # ninguém pegou ainda

    # uma máquina pega o job → a fila mostra quem está executando
    dev = await _device(admin, {"infer": True}, slug="edge-fila")
    got = (await client.post("/v1/agent/claim", headers=_h(dev["token"]))).json()
    assert got["job"]["id"] == job_id
    r = await admin.get("/v1/jobs")
    meu = next(j for j in fila_ok(r) if j["id"] == job_id)
    assert meu["status"] == "claimed"
    assert meu["device"] == "edge-fila"


def fila_ok(r):
    assert r.status_code == 200, r.text
    return r.json()


async def test_cancelar_job_na_fila(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    job_id = await _export_job(admin)

    r = await admin.post(f"/v1/jobs/{job_id}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "canceled"  # na fila → cancela na hora

    # cancelar de novo (já terminal) é recusado
    r = await admin.post(f"/v1/jobs/{job_id}/cancel")
    assert r.status_code == 409


async def test_fila_exige_superusuario(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    async with novo_usuario(admin, client, "fila@teste.com") as (comum, _):
        r = await comum.get("/v1/jobs")
        assert r.status_code == 403


async def test_job_id_malformado_nao_e_500(admin, tmp_path):
    """Id que não é uuid é erro de entrada: recusa na rota, não estoura no cast
    do Postgres antes do 404 que o handler já tem."""
    from .test_datasets import PROJ, _projeto_com_dados
    await _projeto_com_dados(admin, tmp_path)
    r = await admin.get(f"/v1/projects/{PROJ['slug']}/jobs/nao-e-uuid")
    assert r.status_code == 422, r.text
