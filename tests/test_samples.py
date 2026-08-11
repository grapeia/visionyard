"""Aba Dados: listagem, anotação por autor e validação de caixa."""
from __future__ import annotations

import pytest

from .test_datasets import _projeto_com_dados, PROJ
from .test_projects import novo_usuario

pytestmark = pytest.mark.anyio


async def _primeiro(admin):
    r = await admin.get(f"/v1/projects/{PROJ['slug']}/samples")
    return r.json()["itens"][0]["id"]


async def test_lista_e_filtra_por_minha_anotacao(admin, tmp_path):
    """'Pendente' é pendente PARA MIM: o rotulador precisa achar o que falta
    para ele, não o que falta no geral."""
    await _projeto_com_dados(admin, tmp_path)
    r = await admin.get(f"/v1/projects/{PROJ['slug']}/samples")
    d = r.json()
    assert d["total"] == 6

    pend = (await admin.get(
        f"/v1/projects/{PROJ['slug']}/samples?filtro=pendentes")).json()
    anot = (await admin.get(
        f"/v1/projects/{PROJ['slug']}/samples?filtro=anotadas")).json()
    assert len(pend["itens"]) + len(anot["itens"]) == 6
    assert len(anot["itens"]) == 5      # a importação anotou 5


async def test_salvar_anotacao_e_marcar_correcao(admin, tmp_path):
    """Corrigir o que veio pronto é 'correction', não 'manual'.

    A distinção é o que mede depois quanto o humano mudou da sugestão. Se essa
    taxa cair muito, o ciclo virou autoconfirmação.
    """
    await _projeto_com_dados(admin, tmp_path)
    sid = await _primeiro(admin)
    r = await admin.put(
        f"/v1/projects/{PROJ['slug']}/samples/{sid}/annotation",
        json={"boxes": [{"cls": 0, "cx": .5, "cy": .5, "w": .2, "h": .2}]})
    assert r.status_code == 200
    assert r.json()["origin"] == "correction"
    assert r.json()["caixas"] == 1


async def test_caixa_invalida_e_recusada(admin, tmp_path):
    await _projeto_com_dados(admin, tmp_path)
    sid = await _primeiro(admin)
    base = f"/v1/projects/{PROJ['slug']}/samples/{sid}/annotation"
    # classe que não existe no schema
    r = await admin.put(base, json={"boxes": [{"cls": 9, "cx": .5, "cy": .5,
                                               "w": .2, "h": .2}]})
    assert r.status_code == 422
    # centro fora da imagem — sempre erro de conversão ou de arrasto
    r = await admin.put(base, json={"boxes": [{"cls": 0, "cx": 1.8, "cy": .5,
                                               "w": .2, "h": .2}]})
    assert r.status_code == 422
    # largura zero
    r = await admin.put(base, json={"boxes": [{"cls": 0, "cx": .5, "cy": .5,
                                               "w": 0, "h": .2}]})
    assert r.status_code == 422


async def test_lista_vazia_e_anotacao_valida(admin, tmp_path):
    """Salvar sem caixa significa 'olhei e não há nada' — exemplo negativo."""
    await _projeto_com_dados(admin, tmp_path)
    sid = await _primeiro(admin)
    r = await admin.put(f"/v1/projects/{PROJ['slug']}/samples/{sid}/annotation",
                        json={"boxes": []})
    assert r.status_code == 200 and r.json()["caixas"] == 0

    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/samples/{sid}")).json()
    assert d["minha_anotacao"]["boxes"] == []


async def test_nao_sobrescreve_anotacao_de_outro(admin, client, tmp_path):
    """Anotação é por (imagem, autor): é isso que permite medir concordância."""
    await _projeto_com_dados(admin, tmp_path)
    sid = await _primeiro(admin)
    async with novo_usuario(admin, client, "ana@teste.com") as (ana, _):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "ana@teste.com", "role": "annotator"})
        r = await ana.put(
            f"/v1/projects/{PROJ['slug']}/samples/{sid}/annotation",
            json={"boxes": [{"cls": 1, "cx": .1, "cy": .1, "w": .1, "h": .1}]})
        assert r.status_code == 200

        # a minha continua com as caixas originais da importação
        minha = (await admin.get(
            f"/v1/projects/{PROJ['slug']}/samples/{sid}")).json()
        assert len(minha["minha_anotacao"]["boxes"]) == 1
        assert minha["outros_anotaram"] == 1

        # e a dela não mostra as caixas alheias
        dela = (await ana.get(
            f"/v1/projects/{PROJ['slug']}/samples/{sid}")).json()
        assert dela["minha_anotacao"]["boxes"][0]["cls"] == 1
        assert dela["outros_anotaram"] == 1
        assert "boxes" not in str(dela.get("outros_anotaram"))


async def test_viewer_nao_anota(admin, client, tmp_path):
    await _projeto_com_dados(admin, tmp_path)
    sid = await _primeiro(admin)
    async with novo_usuario(admin, client, "leitor@teste.com") as (leitor, _):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "leitor@teste.com", "role": "viewer"})
        r = await leitor.put(
            f"/v1/projects/{PROJ['slug']}/samples/{sid}/annotation",
            json={"boxes": []})
        assert r.status_code == 403


async def test_escopo_por_dataset(admin, tmp_path):
    """O escopo restringe as imagens às origens do dataset — mas a anotação
    continua no sample, valendo para qualquer dataset que a inclua."""
    await _projeto_com_dados(admin, tmp_path)   # 6 imagens numa origem
    # cria um dataset e liga essa origem a ele
    await admin.post(f"/v1/projects/{PROJ['slug']}/datasets",
                     json={"slug": "d1", "name": "D1", "kind": "train"})
    import psycopg
    from .conftest import TEST_DSN
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        src = str(conn.execute(
            "SELECT ds.id FROM data_source ds JOIN project p"
            "  ON p.id = ds.project_id WHERE p.slug=%s LIMIT 1",
            (PROJ["slug"],)).fetchone()[0])
    await admin.put(f"/v1/projects/{PROJ['slug']}/datasets/d1/origens/{src}")

    # escopo do dataset devolve as 6; um dataset vazio devolve 0
    r = await admin.get(f"/v1/projects/{PROJ['slug']}/samples?dataset=d1")
    assert r.json()["total"] == 6
    await admin.post(f"/v1/projects/{PROJ['slug']}/datasets",
                     json={"slug": "vazio", "name": "Vazio", "kind": "train"})
    r = await admin.get(f"/v1/projects/{PROJ['slug']}/samples?dataset=vazio")
    assert r.json()["total"] == 0
    # sem escopo, todas
    r = await admin.get(f"/v1/projects/{PROJ['slug']}/samples")
    assert r.json()["total"] == 6
