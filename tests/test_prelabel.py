"""Pré-tagueamento em lote: enfileirar, alvos, escrita e não-sobrescrita."""
from __future__ import annotations

import pytest

from .test_datasets import DS, PROJ
from .test_models import _produzir_modelo
from .test_training import _device, _h

pytestmark = pytest.mark.anyio


async def _pendentes(admin) -> list:
    r = await admin.get(
        f"/v1/projects/{PROJ['slug']}/samples?dataset={DS['slug']}&filtro=pendentes")
    return r.json()["itens"]


async def _anotadas(admin) -> list:
    r = await admin.get(
        f"/v1/projects/{PROJ['slug']}/samples?dataset={DS['slug']}&filtro=anotadas")
    return r.json()["itens"]


async def _claim_prelabel(admin, client) -> tuple[str, dict]:
    dev = await _device(admin, {"prelabel": True}, slug="pretag-box")
    tok = dev["token"]
    job = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]
    return tok, job


async def test_only_missing_mira_so_pendentes(admin, client, tmp_path):
    """only_missing=true entrega ao agente só quem não tem anotação minha."""
    await _produzir_modelo(admin, client, tmp_path)
    pend = await _pendentes(admin)
    assert len(pend) >= 1

    r = await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel", json={
        "model_slug": "detector", "dataset_slug": DS["slug"], "only_missing": True})
    assert r.status_code == 201, r.text

    tok, job = await _claim_prelabel(admin, client)
    assert job is not None and job["type"] == "prelabel"
    alvos = (await client.get(f"/v1/agent/jobs/{job['id']}/targets",
                              headers=_h(tok))).json()["alvos"]
    ids_alvo = {a["sample_id"] for a in alvos}
    ids_pend = {p["id"] for p in pend}
    assert ids_alvo == ids_pend, "only_missing não filtrou pelas pendentes"


async def test_prelabel_escreve_e_vira_minha_anotacao(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    sid = (await _pendentes(admin))[0]["id"]

    r = await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel", json={
        "model_slug": "detector", "dataset_slug": DS["slug"], "only_missing": True})
    tok, job = await _claim_prelabel(admin, client)
    await client.post(f"/v1/agent/jobs/{job['id']}/heartbeat",
                      json={"progress": {}}, headers=_h(tok))
    r = await client.post(f"/v1/agent/jobs/{job['id']}/prelabel", json={"predicoes": [
        {"sample_id": sid, "boxes": [
            {"cls": 0, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2, "conf": 0.9}]}]},
        headers=_h(tok))
    assert r.json()["gravadas"] == 1

    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/samples/{sid}")).json()
    assert d["minha_anotacao"] is not None
    assert d["minha_anotacao"]["origin"] == "prelabel"
    assert d["minha_anotacao"]["boxes"][0]["cls"] == 0


async def test_predicao_vazia_ignorada(admin, client, tmp_path):
    """Modelo não achou nada → não grava, a imagem segue pendente para um humano."""
    await _produzir_modelo(admin, client, tmp_path)
    sid = (await _pendentes(admin))[0]["id"]
    await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel", json={
        "model_slug": "detector", "dataset_slug": DS["slug"]})
    tok, job = await _claim_prelabel(admin, client)
    r = await client.post(f"/v1/agent/jobs/{job['id']}/prelabel",
                          json={"predicoes": [{"sample_id": sid, "boxes": []}]},
                          headers=_h(tok))
    assert r.json()["gravadas"] == 0
    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/samples/{sid}")).json()
    assert d["minha_anotacao"] is None


async def test_prelabel_nao_sobrescreve_humano(admin, client, tmp_path):
    """Uma anotação humana (import) não é tocada pelo pré-tag."""
    await _produzir_modelo(admin, client, tmp_path)
    alvo = (await _anotadas(admin))[0]          # já tem anotação do admin (import)
    sid = alvo["id"]
    antes = (await admin.get(f"/v1/projects/{PROJ['slug']}/samples/{sid}")).json()
    assert antes["minha_anotacao"]["origin"] in ("import", "manual", "correction")

    await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel", json={
        "model_slug": "detector", "dataset_slug": DS["slug"], "only_missing": False})
    tok, job = await _claim_prelabel(admin, client)
    r = await client.post(f"/v1/agent/jobs/{job['id']}/prelabel", json={"predicoes": [
        {"sample_id": sid, "boxes": [
            {"cls": 1, "cx": 0.1, "cy": 0.1, "w": 0.1, "h": 0.1}]}]},
        headers=_h(tok))
    assert r.json()["gravadas"] == 0, "pré-tag sobrescreveu trabalho humano"
    depois = (await admin.get(f"/v1/projects/{PROJ['slug']}/samples/{sid}")).json()
    assert depois["minha_anotacao"]["origin"] == antes["minha_anotacao"]["origin"]


async def test_prelabel_usa_champion(admin, client, tmp_path):
    """Sem versão explícita, o pré-tag usa o apelido champion."""
    await _produzir_modelo(admin, client, tmp_path)
    await admin.put(
        f"/v1/projects/{PROJ['slug']}/models/detector/aliases/champion",
        json={"version": 1})
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel", json={
        "model_slug": "detector", "dataset_slug": DS["slug"]})
    assert r.status_code == 201
    assert r.json()["model_version"] == 1


async def test_prelabel_apelido_inexistente_404(admin, client, tmp_path):
    """Apelido pedido de propósito é exigência, não sugestão: cair calado para a
    última versão pré-tagearia com um modelo que ninguém escolheu."""
    await _produzir_modelo(admin, client, tmp_path)
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel", json={
        "model_slug": "detector", "dataset_slug": DS["slug"],
        "alias": "challenger"})
    assert r.status_code == 404, r.text
    assert "challenger" in r.json()["detail"]

    # sem apelido pedido, o fallback para a última versão continua valendo
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel", json={
        "model_slug": "detector", "dataset_slug": DS["slug"]})
    assert r.status_code == 201, r.text


async def test_prelabel_exige_engineer(admin, client, tmp_path):
    from .test_projects import novo_usuario
    await _produzir_modelo(admin, client, tmp_path)
    async with novo_usuario(admin, client, "rot@teste.com") as (rot, _):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "rot@teste.com", "role": "annotator"})
        r = await rot.post(f"/v1/projects/{PROJ['slug']}/prelabel", json={
            "model_slug": "detector", "dataset_slug": DS["slug"]})
        assert r.status_code == 403


async def test_prelabel_escopo_de_sample(admin, client, tmp_path):
    """A tecla w: pré-tag de uma imagem só."""
    await _produzir_modelo(admin, client, tmp_path)
    sid = (await _pendentes(admin))[0]["id"]
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel", json={
        "model_slug": "detector", "sample_id": sid})
    assert r.status_code == 201, r.text

    tok, job = await _claim_prelabel(admin, client)
    alvos = (await client.get(f"/v1/agent/jobs/{job['id']}/targets",
                              headers=_h(tok))).json()["alvos"]
    assert [a["sample_id"] for a in alvos] == [sid], "escopo não ficou na imagem"
    await client.post(f"/v1/agent/jobs/{job['id']}/prelabel", json={"predicoes": [
        {"sample_id": sid, "boxes": [
            {"cls": 0, "cx": 0.5, "cy": 0.5, "w": 0.3, "h": 0.3}]}]},
        headers=_h(tok))
    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/samples/{sid}")).json()
    assert d["minha_anotacao"]["origin"] == "prelabel"


async def test_prelabel_escopo_ambiguo_recusa(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    sid = (await _pendentes(admin))[0]["id"]
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel", json={
        "model_slug": "detector", "dataset_slug": DS["slug"], "sample_id": sid})
    assert r.status_code == 422
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel",
                         json={"model_slug": "detector"})
    assert r.status_code == 422


async def test_prelabel_nao_escreve_sample_de_outro_projeto(admin, client, tmp_path):
    """IDOR: sample_id fora do projeto do job não é gravado (INSERT...SELECT)."""
    await _produzir_modelo(admin, client, tmp_path)
    await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel",
                     json={"model_slug": "detector", "dataset_slug": DS["slug"]})
    tok, job = await _claim_prelabel(admin, client)
    alheio = "00000000-0000-0000-0000-000000000000"   # uuid válido, fora do projeto
    r = await client.post(f"/v1/agent/jobs/{job['id']}/prelabel", json={"predicoes": [
        {"sample_id": alheio, "boxes": [
            {"cls": 0, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2}]}]},
        headers=_h(tok))
    assert r.json()["gravadas"] == 0, "escreveu em sample fora do projeto"


async def test_prelabel_escopo_lote_de_ids(admin, client, tmp_path):
    """Escopo por lista de ids (ex.: só a página): mira exatamente os ids."""
    await _produzir_modelo(admin, client, tmp_path)
    ids = [p["id"] for p in (await _pendentes(admin))[:2]]
    assert len(ids) >= 1
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel", json={
        "model_slug": "detector", "sample_ids": ids, "only_missing": True})
    assert r.status_code == 201, r.text
    tok, job = await _claim_prelabel(admin, client)
    alvos = (await client.get(f"/v1/agent/jobs/{job['id']}/targets",
                              headers=_h(tok))).json()["alvos"]
    assert {a["sample_id"] for a in alvos} == set(ids)


async def test_descartar_prelabels_remove_so_rascunho(admin, client, tmp_path):
    """Descartar remove os pré-tags rascunho e preserva o trabalho humano."""
    await _produzir_modelo(admin, client, tmp_path)
    sid = (await _pendentes(admin))[0]["id"]
    humano = (await _anotadas(admin))[0]["id"]     # anotação humana (import)
    await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel", json={
        "model_slug": "detector", "sample_id": sid})
    tok, job = await _claim_prelabel(admin, client)
    await client.post(f"/v1/agent/jobs/{job['id']}/prelabel", json={"predicoes": [
        {"sample_id": sid, "boxes": [
            {"cls": 0, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2}]}]},
        headers=_h(tok))
    base = f"/v1/projects/{PROJ['slug']}/samples"
    anot = (await admin.get(f"{base}/{sid}")).json()["minha_anotacao"]
    assert anot["origin"] == "prelabel"

    r = await admin.post(
        f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}/prelabel/discard")
    assert r.status_code == 200, r.text
    assert r.json()["removidos"] >= 1

    # o rascunho sumiu (imagem volta a pendente); o trabalho humano permanece
    assert (await admin.get(f"{base}/{sid}")).json()["minha_anotacao"] is None
    assert (await admin.get(f"{base}/{humano}")).json()["minha_anotacao"] is not None


async def test_prelabel_id_malformado_nao_e_500(admin):
    """Id que não é uuid é erro de entrada: recusa na validação, não estoura no
    cast do Postgres três camadas adiante.

    Sem projeto montado de propósito: o 422 sai do pydantic antes da primeira
    linha do handler, e `admin` é superusuário — `require_role` o deixa passar
    sem consultar nada. Montar modelo aqui era um minuto de setup que nenhuma
    asserção lia.
    """
    base = f"/v1/projects/{PROJ['slug']}/prelabel"
    r = await admin.post(base, json={"model_slug": "detector",
                                     "sample_id": "nao-e-uuid"})
    assert r.status_code == 422, r.text
    r = await admin.post(base, json={"model_slug": "detector",
                                     "sample_ids": ["nao-e-uuid"]})
    assert r.status_code == 422, r.text


async def test_prelabel_superuser_slug_invalido_404(admin):
    """Superusuário passa por require_role sem checar existência — não pode 500."""
    r = await admin.post("/v1/projects/nao-existe/prelabel",
                         json={"model_slug": "detector", "dataset_slug": "x"})
    assert r.status_code == 404


async def test_prelabel_de_segmentacao_grava_poligono_e_recusa_caixa(admin, client):
    """Modelo -seg devolve contorno; o pré-tag grava `pts`. Caixa num projeto
    de segmentação é agente velho ou modelo errado — recusa o lote inteiro em
    vez de gravar uma anotação que o anotador não sabe abrir."""
    import psycopg

    from .conftest import TEST_DSN
    from .test_training import SEG, _publicar, _segmentacao_congelada
    await _segmentacao_congelada(admin)
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        sid = str(conn.execute(
            "WITH s AS (INSERT INTO sample (project_id, sha256, md5, uri,"
            "           width, height)"
            "  SELECT id, 'def', decode(repeat('cd', 16), 'hex'),"
            "         'gs://bucket/bandas/2.jpg', 640, 480"
            "    FROM project WHERE slug = %s RETURNING id)"
            " INSERT INTO sample_source (sample_id, data_source_id)"
            " SELECT s.id, ds.id FROM s, data_source ds"
            "  JOIN project p ON p.id = ds.project_id WHERE p.slug = %s"
            " RETURNING sample_id", (SEG["slug"], SEG["slug"])).fetchone()[0])

    base = f"/v1/projects/{SEG['slug']}"
    r = await admin.post(f"{base}/runs", json={
        "dataset_slug": DS["slug"], "version": 1, "experiment": "seg",
        "base_model": "yolo11n-seg.pt", "epochs": 3})
    assert r.status_code == 201, r.text
    tok = (await _device(admin, {"train": True}))["token"]
    jid = (await client.post("/v1/agent/claim", headers=_h(tok))).json()["job"]["id"]
    await client.post(f"/v1/agent/jobs/{jid}/model", json={
        "weights_uri": await _publicar(client, tok, jid, "best.pt"),
        "sha256": "cd" * 32, "bytes": 1, "metrics": {}}, headers=_h(tok))
    await client.post(f"/v1/agent/jobs/{jid}/finish",
                      json={"status": "succeeded"}, headers=_h(tok))

    modelo = (await admin.get(f"{base}/models")).json()[0]["slug"]
    r = await admin.post(f"{base}/prelabel", json={
        "model_slug": modelo, "dataset_slug": DS["slug"], "only_missing": True})
    assert r.status_code == 201, r.text
    tok, job = await _claim_prelabel(admin, client)
    url = f"/v1/agent/jobs/{job['id']}/prelabel"

    r = await client.post(url, headers=_h(tok), json={"predicoes": [
        {"sample_id": sid, "boxes": [
            {"cls": 0, "cx": .5, "cy": .5, "w": .2, "h": .2, "conf": .9}]}]})
    assert r.status_code == 422

    pts = [[.1, .1], [.9, .1], [.5, .9]]
    r = await client.post(url, headers=_h(tok), json={"predicoes": [
        {"sample_id": sid, "boxes": [{"cls": 0, "pts": pts, "conf": .8}]}]})
    assert r.status_code == 200, r.text
    assert r.json()["gravadas"] == 1
    d = (await admin.get(f"{base}/samples/{sid}")).json()
    assert d["minha_anotacao"]["boxes"] == [{"cls": 0, "pts": pts, "conf": .8}]


async def test_prelabel_de_deteccao_recusa_poligono(admin, client, tmp_path):
    await _produzir_modelo(admin, client, tmp_path)
    sid = (await _pendentes(admin))[0]["id"]
    await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel", json={
        "model_slug": "detector", "dataset_slug": DS["slug"]})
    tok, job = await _claim_prelabel(admin, client)
    r = await client.post(f"/v1/agent/jobs/{job['id']}/prelabel", headers=_h(tok),
                          json={"predicoes": [{"sample_id": sid, "boxes": [
                              {"cls": 0, "pts": [[.1, .1], [.9, .1], [.5, .9]]}]}]})
    assert r.status_code == 422


async def _filtro(admin, filtro: str) -> set:
    r = await admin.get(f"/v1/projects/{PROJ['slug']}/samples"
                        f"?dataset={DS['slug']}&filtro={filtro}")
    assert r.status_code == 200, r.text
    return {it["id"] for it in r.json()["itens"]}


async def test_pretag_tem_filtro_proprio_ate_alguem_salvar(admin, client, tmp_path):
    """O pré-tag não revisado é rascunho do modelo, não trabalho de quem rotula:
    fica fora de "que eu anotei" e o congelamento não o leva. Precisa então
    de um filtro onde dá para achá-lo, revisar e salvar — e, salvo, ele sai
    dali e passa a contar como anotado (`correction`)."""
    await _produzir_modelo(admin, client, tmp_path)
    sid = (await _pendentes(admin))[0]["id"]
    await admin.post(f"/v1/projects/{PROJ['slug']}/prelabel", json={
        "model_slug": "detector", "dataset_slug": DS["slug"], "only_missing": True})
    tok, job = await _claim_prelabel(admin, client)
    r = await client.post(f"/v1/agent/jobs/{job['id']}/prelabel", json={"predicoes": [
        {"sample_id": sid, "boxes": [
            {"cls": 0, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2, "conf": 0.9}]}]},
        headers=_h(tok))
    assert r.json()["gravadas"] == 1

    assert await _filtro(admin, "pretag") == {sid}
    assert sid not in await _filtro(admin, "anotadas")
    assert sid not in await _filtro(admin, "pendentes")

    r = await admin.put(f"/v1/projects/{PROJ['slug']}/samples/{sid}/annotation",
                        json={"boxes": [{"cls": 0, "cx": .5, "cy": .5,
                                         "w": .2, "h": .2}]})
    assert r.json()["origin"] == "correction"
    assert await _filtro(admin, "pretag") == set()
    assert sid in await _filtro(admin, "anotadas")
