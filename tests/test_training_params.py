"""Parâmetros chegam ao subprocesso sem perder valores nem substituir caminhos."""
import json

import pytest

from agent.train_worker import argumentos_de_treino
from contracts import jobs
from contracts.training import catalogo, importar_texto, normalizar

from .test_training import DS, PROJ, _device, _h, _versao_congelada

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("params", [
    {"lr0": float("nan")}, {"momentum": 1}, {"workers": True},
    {"workers": -1}, {"device": "0,1"}, {"device": "../../peso.pt"},
    {"data": "outro.yaml"}, {"resume": True}, {"cfg": "extra.yaml"},
    {"single_cls": True}, {"save": False}, {"val": False},
    {"freeze": [0, True]}, {"batch": 0}, {"epochs": 10 ** 1000},
])
def test_parametro_invalido_nao_chega_ao_treino(params):
    with pytest.raises(ValueError):
        normalizar(params)


def test_dict_do_dev_importa_sem_executar_codigo():
    r = importar_texto("{'epochs': 300, 'optimizer': 'adam', 'cache': False, "
                       "'freeze': None, 'mosaic': 0.0, 'data': 'externo.yaml', "
                       "'format': 'torchscript', 'auto_augment': False}", "detect")
    assert r["params"] == {"epochs": 300, "optimizer": "Adam", "cache": False,
                           "freeze": None, "mosaic": 0.0}
    assert {i["name"] for i in r["ignored"]} == {"data", "format", "auto_augment"}
    with pytest.raises(ValueError):
        importar_texto("{'epochs': __import__('os').getpid()}", "detect")


@pytest.mark.parametrize("tarefa", ["detect", "segment"])
def test_preset_preserva_configuracao_do_dev_ate_a_chamada_train(tarefa):
    params = catalogo(tarefa)["preset_dev"]
    cfg = argumentos_de_treino({"params": params, "data": "congelado.yaml",
                                "project": "/job/runs"})
    assert all(cfg[k] == v for k, v in params.items())
    assert (cfg["epochs"], cfg["imgsz"], cfg["batch"]) == (300, 416, 80)
    assert cfg["patience"] == 150 and cfg["optimizer"] == "Adam"
    assert cfg["lr0"] == .01 and cfg["mosaic"] == 0 and cfg["scale"] == 0
    assert cfg["name"] == "train" and cfg["project"] == "/job/runs"
    assert cfg["data"] == "congelado.yaml" and cfg["save"] and cfg["val"]
    assert ("overlap_mask" in cfg) == (tarefa == "segment")


def test_config_antiga_do_subprocesso_continua_aceita():
    cfg = argumentos_de_treino({"epochs": 3, "batch": 8, "imgsz": 320,
                                "data": "dataset.yaml", "project": "/runs"})
    assert cfg["epochs"] == 3 and cfg["batch"] == 8 and cfg["imgsz"] == 320
    assert "optimizer" not in cfg and cfg["verbose"] is False


def test_payload_avancado_nao_pode_ser_ignorado_por_agente_v1(monkeypatch):
    def payload(params):
        return jobs.Train("dv", "run", "modelo", "yolo11n.pt", "projeto", "ds", 1,
                          params).como_dict()
    basico = payload({"epochs": 3})
    avancado = payload({"epochs": 3, "patience": 150})
    assert basico["v"] == 1 and avancado["v"] == 2
    assert jobs.Train.de_payload(avancado).params["patience"] == 150
    monkeypatch.setattr(jobs, "PAYLOAD_VERSION", 1)
    assert jobs.Train.de_payload(basico).params == {"epochs": 3}
    with pytest.raises(jobs.PayloadInvalido, match="atualize o agente"):
        jobs.Train.de_payload(avancado)


async def test_api_guarda_e_entrega_parametros_ao_agente(admin, client, tmp_path):
    await _versao_congelada(admin, tmp_path)
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/runs", json={
        "dataset_slug": DS["slug"], "version": 1, "experiment": "personalizado",
        "epochs": 300, "imgsz": 416, "batch": 80,
        "advanced_params": {"optimizer": "adam", "patience": 150,
                             "mosaic": 0, "lr0": .01, "device": 0}})
    assert r.status_code == 201, r.text
    d = await admin.get(f"/v1/projects/{PROJ['slug']}/runs/{r.json()['run_id']}")
    assert d.json()["params"]["optimizer"] == "Adam"
    dev = await _device(admin, {"train": True})
    job = (await client.post("/v1/agent/claim", headers=_h(dev["token"]))).json()["job"]
    assert job["payload"]["v"] == 2
    assert job["payload"]["params"] == d.json()["params"]
    cfg = argumentos_de_treino({"params": job["payload"]["params"],
                                "data": "materializado.yaml", "project": "/runs"})
    assert cfg["patience"] == 150 and cfg["mosaic"] == 0


async def test_api_recusa_parametro_reservado_ou_sobrescrita_do_form(admin, tmp_path):
    await _versao_congelada(admin, tmp_path)
    for extras in [{"data": "outro.yaml"}, {"epochs": 1}, {"overlap_mask": True},
                   {"optimizer": "inexistente"}]:
        r = await admin.post(f"/v1/projects/{PROJ['slug']}/runs", json={
            "dataset_slug": DS["slug"], "version": 1, "experiment": "inválido",
            "advanced_params": extras})
        assert r.status_code == 422, r.text


async def test_catalogo_e_importacao_de_configuracao(admin):
    await admin.post("/v1/projects", json=PROJ)
    url = f"/v1/projects/{PROJ['slug']}/training-options"
    r = await admin.get(url)
    assert r.status_code == 200, r.text
    assert r.json()["preset_dev"]["batch"] == 80
    assert "overlap_mask" not in r.json()["preset_dev"]
    r = await admin.post(url + "/parse", json={"text": json.dumps({
        "optimizer": "adam", "epochs": 300, "project": "caminho"})})
    assert r.status_code == 200, r.text
    assert r.json()["params"] == {"optimizer": "Adam", "epochs": 300}
    assert r.json()["ignored"][0]["name"] == "project"
