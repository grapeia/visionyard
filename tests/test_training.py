"""Fase 3.1: fila de jobs, protocolo do agente e ciclo de treino.

O agente é simulado por HTTP (header X-Agent-Token) — não há dispositivo nos testes.
O que se verifica é o contrato do banco: roteamento por capacidade, claim sem
corrida, e o run refletindo o que o agente reportou.
"""
from __future__ import annotations

import pytest

from .test_datasets import DS, PROJ, _projeto_com_dados, _source_id

pytestmark = pytest.mark.anyio


async def _versao_congelada(admin, tmp_path) -> None:
    """Projeto + dataset com uma origem, congelado em v1."""
    await _projeto_com_dados(admin, tmp_path)
    await admin.post(f"/v1/projects/{PROJ['slug']}/datasets", json=DS)
    src = await _source_id(PROJ["slug"])
    base = f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}"
    await admin.put(f"{base}/sources/{src}")
    r = await admin.post(f"{base}/freeze", json={"descricao": "base"})
    assert r.status_code == 201, r.text


async def _device(admin, caps: dict, slug="trainbox") -> dict:
    r = await admin.post("/v1/devices", json={
        "slug": slug, "name": slug, "device_class": "gpu-server",
        "capabilities": caps})
    assert r.status_code == 201, r.text
    return r.json()


def _h(token: str) -> dict:
    return {"X-Agent-Token": token}


async def _criar_run(admin, base_model="yolo11n.pt") -> dict:
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/runs", json={
        "dataset_slug": DS["slug"], "version": 1, "experiment": "baseline",
        "base_model": base_model, "epochs": 3})
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# Ciclo feliz, ponta a ponta
# --------------------------------------------------------------------------- #

async def test_ciclo_completo_treino(admin, client, tmp_path):
    await _versao_congelada(admin, tmp_path)
    dev = await _device(admin, {"train": True})
    tok = dev["token"]
    run = await _criar_run(admin)

    # registro do agente enriquece capabilities sem apagar as do admin
    r = await client.post("/v1/agent/register",
                          json={"capabilities": {"cuda": "12.2"}}, headers=_h(tok))
    assert r.status_code == 200

    # claim entrega o job de treino
    r = await client.post("/v1/agent/claim", headers=_h(tok))
    job = r.json()["job"]
    assert job is not None and job["type"] == "train"
    assert job["payload"]["run_id"] == run["run_id"]
    jid = job["id"]

    # manifesto: meta com classes + items.jsonl como arquivo
    meta = (await client.get(f"/v1/agent/jobs/{jid}/manifest", headers=_h(tok))).json()
    assert meta["classes"] == ["classe_a", "classe_b"]
    itens = await client.get(f"/v1/agent/jobs/{jid}/manifest/items", headers=_h(tok))
    assert itens.status_code == 200
    assert all(linha.startswith("{")
               for linha in itens.text.strip().splitlines())

    # heartbeat move claimed -> running e não pede cancelamento
    r = await client.post(f"/v1/agent/jobs/{jid}/heartbeat",
                          json={"progress": {"epoch": 1, "de": 3}}, headers=_h(tok))
    assert r.json() == {"cancel": False, "status": "running"}

    # logs e métricas por época
    await client.post(f"/v1/agent/jobs/{jid}/events",
                      json={"eventos": [{"seq": 1, "message": "iniciando"}]},
                      headers=_h(tok))
    await client.post(f"/v1/agent/jobs/{jid}/metrics", json={"metricas": [
        {"epoch": 1, "key": "mAP50-95", "value": 0.40},
        {"epoch": 2, "key": "mAP50-95", "value": 0.62},
        {"epoch": 2, "key": "mAP50", "value": 0.81}]}, headers=_h(tok))

    # registro do modelo produzido
    r = await client.post(f"/v1/agent/jobs/{jid}/model", json={
        "weights_uri": "gs://bucket/models/best.pt", "sha256": "ab" * 32,
        "bytes": 5_000_000, "metrics": {"mAP50-95": 0.62}}, headers=_h(tok))
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1 and r.json()["model_slug"] == "detector"

    # fim
    r = await client.post(f"/v1/agent/jobs/{jid}/finish",
                          json={"status": "succeeded"}, headers=_h(tok))
    assert r.json()["status"] == "succeeded"

    # o run reflete tudo
    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/runs/{run['run_id']}")).json()
    assert d["job_status"] == "succeeded"
    assert d["run_status"] == "finished"
    assert d["best_epoch"] == 2
    assert d["metrics"]["mAP50-95"] == 0.62
    assert d["model_version"]["version"] == 1
    assert d["model_version"]["weights_uri"] == "gs://bucket/models/best.pt"

    # stream incremental devolve eventos e métricas
    s = (await admin.get(
        f"/v1/projects/{PROJ['slug']}/runs/{run['run_id']}/stream")).json()
    assert len(s["eventos"]) == 1
    assert len(s["metricas"]) == 3

    # device voltou a idle
    devs = (await admin.get("/v1/devices")).json()
    assert next(x for x in devs if x["slug"] == "trainbox")["status"] == "idle"


# --------------------------------------------------------------------------- #
# Roteamento por capacidade
# --------------------------------------------------------------------------- #

async def test_device_sem_capacidade_nao_reclama_treino(admin, client, tmp_path):
    """dispositivo de borda (só infer) não pode pegar job de treino:
    capabilities @> requires falha, e o claim devolve nada."""
    await _versao_congelada(admin, tmp_path)
    await _criar_run(admin)
    edge = await _device(admin, {"infer": True}, slug="edge-1")
    r = await client.post("/v1/agent/claim", headers=_h(edge["token"]))
    assert r.json()["job"] is None


async def test_claim_nao_entrega_o_mesmo_job_a_dois(admin, client, tmp_path):
    """Um job, um dono. O segundo claim não vê nada."""
    await _versao_congelada(admin, tmp_path)
    await _criar_run(admin)
    a = await _device(admin, {"train": True}, slug="box-a")
    b = await _device(admin, {"train": True}, slug="box-b")
    r1 = await client.post("/v1/agent/claim", headers=_h(a["token"]))
    r2 = await client.post("/v1/agent/claim", headers=_h(b["token"]))
    pegou = [r for r in (r1, r2) if r.json()["job"] is not None]
    assert len(pegou) == 1, "dois agentes pegaram o mesmo job"


async def test_draining_nao_reclama(admin, client, tmp_path):
    """Tirar o device do pool: para de pegar, mas o token continua válido."""
    await _versao_congelada(admin, tmp_path)
    await _criar_run(admin)
    dev = await _device(admin, {"train": True})
    await admin.post("/v1/devices/trainbox/status?estado=draining")
    r = await client.post("/v1/agent/claim", headers=_h(dev["token"]))
    assert r.json()["job"] is None


# --------------------------------------------------------------------------- #
# Cancelamento
# --------------------------------------------------------------------------- #

async def test_cancelar_na_fila_cancela_na_hora(admin, client, tmp_path):
    await _versao_congelada(admin, tmp_path)
    run = await _criar_run(admin)
    r = await admin.post(
        f"/v1/projects/{PROJ['slug']}/runs/{run['run_id']}/cancel")
    assert r.json()["status"] == "canceled"
    # e já não há nada para reclamar
    dev = await _device(admin, {"train": True})
    r = await client.post("/v1/agent/claim", headers=_h(dev["token"]))
    assert r.json()["job"] is None


async def test_cancelar_rodando_sinaliza_no_heartbeat(admin, client, tmp_path):
    await _versao_congelada(admin, tmp_path)
    run = await _criar_run(admin)
    dev = await _device(admin, {"train": True})
    tok = dev["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    await client.post(f"/v1/agent/jobs/{job['id']}/heartbeat",
                      json={"progress": {}}, headers=_h(tok))
    # usuário pede cancelamento com o job já rodando
    await admin.post(f"/v1/projects/{PROJ['slug']}/runs/{run['run_id']}/cancel")
    r = await client.post(f"/v1/agent/jobs/{job['id']}/heartbeat",
                          json={"progress": {}}, headers=_h(tok))
    assert r.json()["cancel"] is True


# --------------------------------------------------------------------------- #
# Fronteiras de posse e permissão
# --------------------------------------------------------------------------- #

async def test_agente_nao_mexe_em_job_alheio(admin, client, tmp_path):
    await _versao_congelada(admin, tmp_path)
    await _criar_run(admin)
    a = await _device(admin, {"train": True}, slug="box-a")
    b = await _device(admin, {"train": True}, slug="box-b")
    job = (await client.post("/v1/agent/claim", headers=_h(a["token"]))).json()["job"]
    # B tenta reportar evento no job de A
    r = await client.post(f"/v1/agent/jobs/{job['id']}/events",
                          json={"eventos": [{"seq": 1, "message": "x"}]},
                          headers=_h(b["token"]))
    assert r.status_code == 404


async def test_register_nao_escala_capacidade(admin, client, tmp_path):
    """Um dispositivo (infer) não pode se declarar `train` no register e passar a
    reclamar treino. O agente só declara diagnóstico; roteamento é do admin."""
    await _versao_congelada(admin, tmp_path)
    await _criar_run(admin)
    edge = await _device(admin, {"infer": True}, slug="edge-1")
    tok = edge["token"]
    # tenta injetar a flag de treino + um diagnóstico legítimo
    await client.post("/v1/agent/register",
                      json={"capabilities": {"train": True, "cuda": "12.2"}},
                      headers=_h(tok))
    # a flag foi ignorada: continua sem pegar treino
    r = await client.post("/v1/agent/claim", headers=_h(tok))
    assert r.json()["job"] is None, "device escalou de infer para train"
    # mas o diagnóstico entrou
    devs = (await admin.get("/v1/devices")).json()
    d = next(x for x in devs if x["slug"] == "edge-1")
    assert d["capabilities"].get("cuda") == "12.2"
    assert "train" not in d["capabilities"]


async def test_token_invalido_401(client):
    r = await client.post("/v1/agent/claim", headers=_h("nao-existe"))
    assert r.status_code == 401


async def test_criar_device_exige_superuser(admin, client, tmp_path):
    from .test_projects import novo_usuario
    await admin.post("/v1/projects", json=PROJ)
    async with novo_usuario(admin, client, "eng@teste.com") as (eng, _):
        r = await eng.post("/v1/devices", json={
            "slug": "x", "name": "x", "device_class": "y"})
        assert r.status_code == 403


async def test_run_exige_versao_congelada(admin, tmp_path):
    await _projeto_com_dados(admin, tmp_path)
    await admin.post(f"/v1/projects/{PROJ['slug']}/datasets", json=DS)
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/runs", json={
        "dataset_slug": DS["slug"], "version": 1, "experiment": "x"})
    assert r.status_code == 404


async def test_mesmo_slug_cadastrado_duas_vezes_ao_mesmo_tempo(admin):
    """Ler-e-inserir de novo, agora no cadastro de máquina.

    Duas requisições veem "não existe" — READ COMMITTED dá snapshot novo a cada
    comando, mesmo dentro de uma transação — e a segunda estoura no UNIQUE de
    `device.slug`, virando 500 no lugar do 409. Quem decide é o índice.
    """
    import asyncio

    corpo = {"slug": "gemea", "name": "Gêmea", "device_class": "gpu-server",
             "capabilities": {"train": True}}
    r1, r2 = await asyncio.gather(admin.post("/v1/devices", json=corpo),
                                  admin.post("/v1/devices", json=corpo))
    assert sorted([r1.status_code, r2.status_code]) == [201, 409], \
        (r1.status_code, r1.text, r2.status_code, r2.text)
    assert len((await admin.get("/v1/devices")).json()) == 1


async def test_excluir_device_sem_historico(admin, client, tmp_path):
    """Máquina que nunca rodou nada pode ser excluída."""
    await _device(admin, {"infer": True}, slug="descartavel")
    r = await admin.delete("/v1/devices/descartavel")
    assert r.status_code == 204
    devs = (await admin.get("/v1/devices")).json()
    assert all(d["slug"] != "descartavel" for d in devs)


async def test_excluir_device_com_job_recusa(admin, client, tmp_path):
    """Máquina com job no histórico não é excluída — preserva a linhagem."""
    await _versao_congelada(admin, tmp_path)
    await _criar_run(admin)
    dev = await _device(admin, {"train": True}, slug="trabalhou")
    # reclama um job → cria job.device_id apontando para ela
    job = (await client.post("/v1/agent/claim", headers=_h(dev["token"]))).json()["job"]
    assert job is not None
    r = await admin.delete("/v1/devices/trabalhou")
    assert r.status_code == 409
    assert "histórico" in r.json()["detail"]


async def test_excluir_device_exige_superuser(admin, client):
    from .test_projects import novo_usuario
    await _device(admin, {"infer": True}, slug="protegida")
    async with novo_usuario(admin, client, "ze@teste.com") as (ze, _):
        r = await ze.delete("/v1/devices/protegida")
        assert r.status_code == 403


async def test_metrica_nao_finita_e_descartada(admin, client, tmp_path):
    """NaN/inf nas primeiras épocas não podem quebrar o run.

    O corpo vai cru com os tokens NaN/Infinity (que o parser aceita) porque o
    encoder JSON do cliente os recusaria — o mesmo motivo pelo qual o agente
    quebrava ao tentar enviá-los.
    """
    await _versao_congelada(admin, tmp_path)
    run = await _criar_run(admin)
    dev = await _device(admin, {"train": True})
    tok = dev["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    corpo = ('{"metricas": [{"epoch": 1, "key": "mAP50-95", "value": NaN},'
             ' {"epoch": 1, "key": "box_loss", "value": Infinity},'
             ' {"epoch": 2, "key": "mAP50-95", "value": 0.5}]}')
    r = await client.post(f"/v1/agent/jobs/{job['id']}/metrics", content=corpo,
                          headers={**_h(tok), "content-type": "application/json"})
    assert r.status_code == 200, r.text
    assert r.json()["gravadas"] == 1        # só a finita entrou
    s = (await admin.get(
        f"/v1/projects/{PROJ['slug']}/runs/{run['run_id']}/stream")).json()
    assert len(s["metricas"]) == 1
    assert s["metricas"][0]["value"] == 0.5


async def test_reaper_encerra_job_orfao(admin, client, tmp_path):
    """Job 'running' órfão no limite de tentativas vira terminal no próximo
    claim — senão ficaria preso para sempre."""
    import psycopg

    from .conftest import TEST_DSN
    await admin.post("/v1/projects", json=PROJ)
    with psycopg.connect(TEST_DSN, autocommit=True) as c:
        pid = c.execute("SELECT id FROM project WHERE slug=%s",
                        (PROJ["slug"],)).fetchone()[0]
        jid = c.execute(
            "INSERT INTO job (project_id, type, payload, requires, status,"
            " attempts, max_attempts, heartbeat_at)"
            " VALUES (%s,'train','{}'::jsonb,'{}'::jsonb,'running',2,2,"
            " now() - interval '10 min') RETURNING id", (pid,)).fetchone()[0]
    # qualquer claim dispara o reaper
    dev = await _device(admin, {"train": True})
    await client.post("/v1/agent/claim", headers=_h(dev["token"]))
    with psycopg.connect(TEST_DSN, autocommit=True) as c:
        st = c.execute("SELECT status FROM job WHERE id=%s", (jid,)).fetchone()[0]
    assert st == "failed", f"job órfão não foi encerrado: {st}"


async def test_finish_nao_ressuscita_job_ja_encerrado(admin, client, tmp_path):
    """O agente que volta de uma partição de rede não desfaz um cancelamento.

    O reaper (e o cancelamento na fila) deixam o job terminal mas mantêm o
    `device_id`: sem guarda de estado, o `finish` atrasado da máquina casaria
    pelo par (id, device_id) e um treino que o usuário cancelou apareceria como
    concluído com sucesso.
    """
    import psycopg

    from .conftest import TEST_DSN
    await _versao_congelada(admin, tmp_path)
    run = await _criar_run(admin)
    dev = await _device(admin, {"train": True})
    tok = dev["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    await client.post(f"/v1/agent/jobs/{job['id']}/heartbeat",
                      json={"progress": {}}, headers=_h(tok))

    # a máquina some; o usuário cancela e o reaper encerra o job no limite
    await admin.post(f"/v1/projects/{PROJ['slug']}/runs/{run['run_id']}/cancel")
    with psycopg.connect(TEST_DSN, autocommit=True) as c:
        c.execute("UPDATE job SET heartbeat_at = now() - interval '10 min',"
                  " attempts = max_attempts WHERE id = %s", (job["id"],))
    await client.post("/v1/agent/claim", headers=_h(tok))   # dispara o reaper

    # a máquina volta e reporta sucesso do treino que já foi cancelado
    r = await client.post(f"/v1/agent/jobs/{job['id']}/finish",
                          json={"status": "succeeded", "result": {}},
                          headers=_h(tok))
    assert r.status_code == 404, r.text

    with psycopg.connect(TEST_DSN, autocommit=True) as c:
        js = c.execute("SELECT status FROM job WHERE id=%s",
                       (job["id"],)).fetchone()[0]
        rs = c.execute("SELECT status FROM training_run WHERE id=%s",
                       (run["run_id"],)).fetchone()[0]
    assert js == "canceled", f"job cancelado virou {js}"
    assert rs == "stopped", f"run parado virou {rs}"


async def test_train_devices_lista_para_quem_vai_escolher(admin, client, tmp_path):
    """A lista de máquinas-alvo tem de responder a quem dispara o treino.

    A rota não tem `{project_slug}`, então a dependência de papel por projeto
    pedia um caminho que não existe e devolvia 422 para todo mundo — o diálogo
    de novo treino ficava sem máquina para fixar, e o `.catch` do front engolia.
    """
    from .test_projects import novo_usuario
    await _projeto_com_dados(admin, tmp_path)
    await _device(admin, {"train": True}, slug="gpu-1")
    await _device(admin, {"infer": True}, slug="borda-1")

    r = await admin.get("/v1/train-devices")
    assert r.status_code == 200, r.text
    assert [d["slug"] for d in r.json()] == ["gpu-1"], "só quem sabe treinar"
    assert set(r.json()[0]) == {"slug", "name", "status"}, "sem token nem caps"

    async with novo_usuario(admin, client, "eng@teste.com") as (eng, _):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "eng@teste.com", "role": "engineer"})
        assert (await eng.get("/v1/train-devices")).status_code == 200
