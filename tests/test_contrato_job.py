"""O contrato do payload de job — a segunda API do sistema.

Quem enfileira é a Control API; quem lê é o agente, noutra máquina e noutra
cadência de atualização. Antes destes testes, renomear uma chave do payload
passava no `ruff`, no `mypy` e na suíte inteira, e só falhava horas depois como
`KeyError` num dispositivo — marcando o job como `failed` com uma mensagem que
não dizia que o contrato tinha quebrado.

Cada caso enfileira pela rota de verdade, reclama pelo protocolo do agente e lê
o payload com o mesmo parser que o executor do agente usa.
"""
from __future__ import annotations

import pytest

from contracts import jobs

from .test_datasets import DS, PROJ
from .test_models import _produzir_modelo
from .test_training import _criar_run, _device, _h, _versao_congelada

pytestmark = pytest.mark.anyio


async def _claim(admin, client, caps: dict, slug: str) -> dict:
    dev = await _device(admin, caps, slug=slug)
    job = (await client.post("/v1/agent/claim",
                             headers=_h(dev["token"]))).json()["job"]
    assert job is not None, "nada na fila para reclamar"
    return job


async def test_train_enfileirado_e_o_que_o_agente_le(admin, client, tmp_path):
    await _versao_congelada(admin, tmp_path)
    run = await _criar_run(admin)
    job = await _claim(admin, client, {"train": True}, "trainbox")

    p = jobs.Train.de_payload(job["payload"])
    assert p.run_id == run["run_id"]
    assert p.dataset_slug == DS["slug"] and p.version == 1
    assert p.model_slug == "detector" and p.base_model == "yolo11n.pt"
    assert p.project_slug == PROJ["slug"] and p.dataset_version_id


async def test_prelabel_enfileirado_e_o_que_o_agente_le(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel", json={
        "model_slug": "detector", "dataset_slug": DS["slug"], "conf": 0.4})
    assert r.status_code == 201, r.text
    job = await _claim(admin, client, {"prelabel": True}, "pretag-box")

    p = jobs.Prelabel.de_payload(job["payload"])
    assert p.weights_uri.startswith("gs://")
    assert p.dataset_slug == DS["slug"] and p.conf == 0.4
    assert p.only_missing is True
    assert p.sample_id is None and p.sample_ids is None
    assert p.model_version_id and p.schema_id


async def test_export_enfileirado_e_o_que_o_agente_le(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    r = await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/export",
        json={"format": "engine", "precision": "int8", "dla": True})
    assert r.status_code == 201, r.text
    job = await _claim(admin, client, {"infer": True}, "edge-export")

    p = jobs.Export.de_payload(job["payload"])
    assert p.format == "engine" and p.precision == "int8" and p.imgsz == 640
    assert p.dla is True and p.dynamic is False   # DLA não aceita dinâmico
    assert p.model_slug == "detector" and p.version == 1 and p.weights_uri


async def test_evaluate_enfileirado_e_o_que_o_agente_le(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    r = await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/evaluate",
        json={"dataset_slug": DS["slug"], "dataset_version": 1})
    assert r.status_code == 201, r.text
    job = await _claim(admin, client, {"infer": True}, "eval-box")

    p = jobs.Evaluate.de_payload(job["payload"])
    assert p.artifact_uri and p.model_version_id and p.dataset_version_id
    assert p.format == "pt" and p.rotulo == "pt" and p.imgsz == 640
    # 'pt' mede o peso da versão; build só existe para onnx/engine
    assert p.model_build_id is None


# --------------------------------------------------------------------------- #
# O que o parser faz quando o payload não é o que se espera
# --------------------------------------------------------------------------- #

def test_chave_faltando_diz_qual_e_de_que_job():
    with pytest.raises(jobs.PayloadInvalido) as e:
        jobs.Export.de_payload({"model_version_id": "x", "model_slug": "d",
                                "project_slug": "p", "version": 1})
    assert "weights_uri" in str(e.value) and "export" in str(e.value)


def test_payload_de_versao_futura_manda_atualizar_o_agente():
    """Controlador novo, agente velho: a mensagem tem de dizer o que fazer —
    um `KeyError` de chave nova diria só que faltou alguma coisa."""
    with pytest.raises(jobs.PayloadInvalido) as e:
        jobs.Export.de_payload({"v": jobs.PAYLOAD_VERSION + 1,
                                "model_version_id": "x", "weights_uri": "gs://b/w.pt",
                                "model_slug": "d", "project_slug": "p",
                                "version": 1})
    assert "atualize o agente" in str(e.value)


def test_chave_desconhecida_e_ignorada():
    """Mesma assimetria pelo outro lado: o roteiro de deploy sobe o controlador
    antes dos agentes, então um campo novo tem de atravessar o agente velho sem
    derrubar o job."""
    p = jobs.Export.de_payload({"model_version_id": "x", "weights_uri": "gs://b/w.pt",
                                "model_slug": "d", "project_slug": "p", "version": 1,
                                "campo_que_ainda_nao_existia": 42})
    assert p.model_slug == "d"


def test_job_sem_versao_e_v1():
    """Job enfileirado antes desta mudança está no banco e vai ser reclamado
    depois dela: ausência de `v` é v1, não erro."""
    p = jobs.Export.de_payload({"model_version_id": "x", "weights_uri": "gs://b/w.pt",
                                "model_slug": "d", "project_slug": "p", "version": 1})
    assert p.format == "engine"     # o default de hoje, preservado


def test_todo_tipo_com_contrato_tem_executor_ligado_nele():
    """Guarda contra a regressão que este teste nasceu para pegar: a ligação de
    um executor ao contrato foi revertida por um `git checkout` de conferência,
    e nada acusou — o `mypy` não vê tipo em `job["payload"]`, que é `Any`, e
    nenhum teste roda os executores (dependem de GPU e de ultralytics).
    """
    from pathlib import Path

    raiz = Path(__file__).resolve().parents[1] / "agent"
    for tipo, contrato in jobs.POR_TIPO.items():
        fonte = (raiz / f"{tipo}.py").read_text()
        assert f"{contrato.__name__}.de_payload(" in fonte, tipo
        assert 'payload["' not in fonte, f"{tipo} ainda lê o payload cru"


async def test_rota_de_agente_so_serve_job_do_seu_tipo(admin, client, tmp_path):
    """Cada rota do agente pertence a um tipo de job, e é ela quem confere.

    Antes, o que barrava um agente chamando `/build` num job de treino era a
    ausência de `model_version_id` no payload — um 404 que saía por acidente, e
    que sumiria ao ler o payload pelo contrato. Pior, `/targets` num job de
    treino respondia 200 com lista vazia: o escopo do pré-tag era lido com
    `.get()`, e `None` virava uma consulta que não casa com nada.
    """
    await _versao_congelada(admin, tmp_path)
    await _criar_run(admin)
    dev = await _device(admin, {"train": True, "prelabel": True,
                                "export": True, "evaluate": True}, slug="tudo")
    tok = dev["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    assert job["type"] == "train"
    jid = job["id"]

    alheias = [
        ("get", f"/v1/agent/jobs/{jid}/targets", None),
        ("post", f"/v1/agent/jobs/{jid}/prelabel", {"predicoes": []}),
        ("post", f"/v1/agent/jobs/{jid}/build",
         {"format": "engine", "uri": "gs://b/x.engine", "sha256": "ab" * 32}),
        ("post", f"/v1/agent/jobs/{jid}/evaluation", {"metrics": {}}),
    ]
    for metodo, url, corpo in alheias:
        r = (await client.get(url, headers=_h(tok)) if metodo == "get"
             else await client.post(url, json=corpo, headers=_h(tok)))
        assert r.status_code == 404, f"{metodo} {url} devolveu {r.status_code}"

    # e o que é do tipo certo continua passando
    assert (await client.get(f"/v1/agent/jobs/{jid}/manifest",
                             headers=_h(tok))).status_code == 200


async def test_manifesto_serve_treino_e_avaliacao_e_mais_ninguem(
        admin, client, tmp_path):
    """A única rota do agente com dois donos — e ter dois não é ter todos.

    O treino aprende com o manifesto e a avaliação mede contra ele; os dois
    payloads carregam `dataset_version_id`. Ler pelo contrato de treino, como
    a rota passou a fazer, recusava com 404 toda avaliação antes de ela baixar
    o benchmark (o caso feliz está em `test_evaluate.py`). Aqui fica o outro
    lado: aceitar dois tipos não é aceitar qualquer um.
    """
    await _produzir_modelo(admin, client, tmp_path)
    r = await admin.post(
        f"/v1/projects/{PROJ['slug']}/models/detector/versions/1/export",
        json={"format": "engine"})
    assert r.status_code == 201, r.text
    dev = await _device(admin, {"infer": True}, slug="edge-export")
    tok = dev["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    assert job["type"] == "export"

    for url in (f"/v1/agent/jobs/{job['id']}/manifest",
                f"/v1/agent/jobs/{job['id']}/manifest/items"):
        r = await client.get(url, headers=_h(tok))
        assert r.status_code == 404, url
