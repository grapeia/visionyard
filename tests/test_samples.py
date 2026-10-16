"""Aba Dados: listagem, anotação por autor e validação de caixa."""
from __future__ import annotations

import pytest

from .test_datasets import PROJ, _projeto_com_dados
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


async def test_total_conta_o_que_a_grade_mostra(admin, tmp_path):
    """O total é o rodapé da grade filtrada, não do projeto: contar só pelo
    escopo escreveria "6 imagens" sobre uma grade de 1."""
    await _projeto_com_dados(admin, tmp_path)   # 6 imagens, 5 anotadas
    for filtro, n in (("pendentes", 1), ("anotadas", 5), ("todas", 6)):
        d = (await admin.get(
            f"/v1/projects/{PROJ['slug']}/samples?filtro={filtro}")).json()
        assert d["total"] == n == len(d["itens"]), filtro


async def test_salvar_anotacao_e_marcar_correcao(admin, tmp_path):
    """Corrigir o que veio pronto é 'correction', não 'manual'.

    A distinção é o que mede depois quanto o humano mudou da sugestão. Se essa
    taxa cair muito, o ciclo virou autoconfirmação.
    """
    await _projeto_com_dados(admin, tmp_path)
    sid = await _primeiro(admin)
    base = f"/v1/projects/{PROJ['slug']}/samples/{sid}/annotation"
    r = await admin.put(
        base, json={"boxes": [{"cls": 0, "cx": .5, "cy": .5, "w": .2, "h": .2}]})
    assert r.status_code == 200
    assert r.json()["origin"] == "correction"
    assert r.json()["caixas"] == 1

    # Continuar mexendo na mesma caixa não apaga a procedência: se o segundo
    # save virasse 'manual', a taxa de correção zeraria justo nas imagens que
    # deram mais trabalho — o oposto do que ela mede.
    r = await admin.put(
        base, json={"boxes": [{"cls": 0, "cx": .4, "cy": .5, "w": .2, "h": .2}]})
    assert r.json()["origin"] == "correction"


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


async def test_sample_id_malformado_nao_e_500(admin, tmp_path):
    """Id que não é uuid é erro de entrada: recusa na rota, não estoura no cast
    do Postgres antes do 404 que o handler já tem."""
    await _projeto_com_dados(admin, tmp_path)
    base = f"/v1/projects/{PROJ['slug']}/samples/nao-e-uuid"
    assert (await admin.get(base)).status_code == 422
    r = await admin.put(f"{base}/annotation", json={"boxes": []})
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
    await admin.put(f"/v1/projects/{PROJ['slug']}/datasets/d1/sources/{src}")

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


async def test_pagina_alem_do_fim_ainda_conta_o_total(admin, tmp_path):
    """A grade vazia não zera o rodapé: quem paginou longe demais precisa ver
    que ainda há 6 imagens para saber que é só voltar."""
    await _projeto_com_dados(admin, tmp_path)
    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/samples?pagina=5")).json()
    assert d["itens"] == [] and d["total"] == 6


async def test_resposta_e_contrato_e_nao_a_linha_do_banco(admin, tmp_path,
                                                          monkeypatch):
    """Coluna acrescentada a um SELECT não viaja para o navegador.

    O que a rota devolve é o `response_model`, não o `dict` que o serviço
    montou — é o que impede o schema do banco de virar API sem ninguém decidir.
    """
    await _projeto_com_dados(admin, tmp_path)
    from api.services import samples as svc

    original = svc.listar

    async def com_coluna_nova(*a, **kw):
        pagina = await original(*a, **kw)
        pagina["segredo_do_banco"] = "x"
        pagina["itens"][0]["coluna_nova"] = "y"
        return pagina

    monkeypatch.setattr(svc, "listar", com_coluna_nova)
    d = (await admin.get(f"/v1/projects/{PROJ['slug']}/samples")).json()
    assert "segredo_do_banco" not in d
    assert "coluna_nova" not in d["itens"][0]
    assert d["total"] == 6          # e o que é contrato continua lá


# --------------------------------------------------------------------------- #
# Segmentação: a anotação é polígono, não caixa
# --------------------------------------------------------------------------- #

SEG = {"slug": "carcaca", "name": "Carcaça", "task_type": "segment",
       "classes": ["banda", {"name": "hematoma", "inside": "banda"}]}
TRI = [[.1, .1], [.9, .1], [.5, .9]]


async def _sample_de_segmentacao(admin) -> str:
    import psycopg

    from .conftest import TEST_DSN
    assert (await admin.post("/v1/projects", json=SEG)).status_code == 201
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO sample (project_id, sha256, uri, width, height)"
            " SELECT id, 'abc', 'gs://bucket/banda.jpg', 640, 480"
            "   FROM project WHERE slug = %s RETURNING id",
            (SEG["slug"],)).fetchone()
    return str(row[0])


async def test_segmentacao_grava_poligono(admin):
    sid = await _sample_de_segmentacao(admin)
    base = f"/v1/projects/{SEG['slug']}/samples/{sid}"
    r = await admin.put(f"{base}/annotation", json={"boxes": [
        {"cls": 0, "pts": TRI}, {"cls": 1, "pts": [[.2, .2], [.4, .2], [.3, .4]]}]})
    assert r.status_code == 200, r.text
    assert r.json()["caixas"] == 2

    d = (await admin.get(base)).json()
    assert d["minha_anotacao"]["boxes"][0] == {"cls": 0, "pts": TRI}


@pytest.mark.parametrize("item", [
    {"cls": 0, "pts": [[.1, .1], [.9, .1]]},              # dois pontos não é área
    {"cls": 0, "pts": [[.1, .1], [1.4, .1], [.5, .9]]},   # vértice fora da imagem
    {"cls": 0, "pts": [[.1, .1, .3], [.9, .1], [.5, .9]]},  # ponto com 3 números
    {"cls": 5, "pts": TRI},                               # classe que não existe
    {"cls": 0, "pts": [[.2, .2], [.2, .2], [.2, .2]]},    # pontos repetidos
    {"cls": 0, "pts": [[.1, .1], [.5, .5], [.9, .9]]},    # colineares
])
async def test_poligono_invalido_e_recusado(admin, item):
    sid = await _sample_de_segmentacao(admin)
    r = await admin.put(f"/v1/projects/{SEG['slug']}/samples/{sid}/annotation",
                        json={"boxes": [item]})
    assert r.status_code == 422, r.text


async def test_caixa_nao_entra_em_projeto_de_segmentacao(admin):
    """Misturar as formas deixaria o manifesto com linhas de 5 números no meio
    das de polígono, e o YOLO-seg as leria como polígono de 2 pontos."""
    sid = await _sample_de_segmentacao(admin)
    r = await admin.put(f"/v1/projects/{SEG['slug']}/samples/{sid}/annotation",
                        json={"boxes": [{"cls": 0, "cx": .5, "cy": .5,
                                         "w": .2, "h": .2}]})
    assert r.status_code == 422
    assert "polígono" in r.json()["detail"]


async def test_poligono_nao_entra_em_projeto_de_deteccao(admin, tmp_path):
    await _projeto_com_dados(admin, tmp_path)
    sid = await _primeiro(admin)
    r = await admin.put(f"/v1/projects/{PROJ['slug']}/samples/{sid}/annotation",
                        json={"boxes": [{"cls": 0, "pts": TRI}]})
    assert r.status_code == 422
    assert "caixa" in r.json()["detail"]


def test_poligono_instanciado_e_reconhecido():
    """O discriminador olha a chave no JSON e o tipo na instância: só a chave
    fazia o `model_dump` da própria resposta cair em `Caixa`."""
    from api.routers.samples import AnotacaoIn, Poligono
    a = AnotacaoIn(boxes=[Poligono(cls=0, pts=[(0, 0), (1, 0), (0, 1)])])
    assert a.model_dump()["boxes"][0]["pts"][1] == (1, 0)
