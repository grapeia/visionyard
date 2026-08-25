"""Datasets: origens múltiplas, congelamento, manifesto e diff."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import psycopg
import pytest
from fastapi import HTTPException

from api.services.datasets import assign_splits, build_manifest

from .conftest import TEST_DSN
from .test_imports import _dump

pytestmark = pytest.mark.anyio

PROJ = {"slug": "linha-de-inspecao", "name": "Linha de inspeção", "task_type": "detect",
        "classes": ["classe_a", "classe_b"]}
DS = {"slug": "linha-a", "name": "Linha A", "kind": "train"}


# --------------------------------------------------------------------------- #
# Split — a razão de existir do congelamento
# --------------------------------------------------------------------------- #

def test_split_e_deterministico():
    """Mesma semente, mesmo split. É o que torna dois treinos comparáveis."""
    def splits():
        itens = [{"sample_id": f"s{i}", "metadata": {}} for i in range(100)]
        assign_splits(itens, {"method": "random", "seed": 42, "val_ratio": 0.2})
        return [i["split"] for i in itens]
    assert splits() == splits()


def test_split_respeita_proporcao():
    itens = [{"sample_id": f"s{i}", "metadata": {}} for i in range(100)]
    assign_splits(itens, {"method": "random", "seed": 7, "val_ratio": 0.2})
    assert sum(1 for i in itens if i["split"] == "val") == 20


def test_split_por_grupo_nao_vaza_entre_treino_e_validacao():
    """Frames do mesmo vídeo não podem cair dos dois lados.

    Se caírem, o modelo 'acerta' na validação uma cena que já viu no treino e
    o mAP infla — a métrica mente sem que nada pareça errado.
    """
    itens = [{"sample_id": f"s{i}", "metadata": {"lote": f"video_{i // 10}"}}
             for i in range(100)]
    assign_splits(itens, {"method": "by_group", "seed": 42,
                          "val_ratio": 0.2, "group_by": "metadata.lote"})
    por_grupo = {}
    for i in itens:
        por_grupo.setdefault(i["metadata"]["lote"], set()).add(i["split"])
    assert all(len(s) == 1 for s in por_grupo.values()), \
        "um grupo ficou dividido entre train e val"


def test_manifesto_tem_hash_e_stats():
    itens = [{"sample_id": "s1", "uri": "gs://b/1.jpg", "md5": b"\x01" * 16,
              "width": 100, "height": 80, "split": "train",
              "boxes": [{"cls": 0, "cx": .5, "cy": .5, "w": .2, "h": .2}]}]
    jsonl, meta = build_manifest(itens, ["classe_a", "classe_b"],
                                 ["gs://b/"], 1, "primeira", {"method": "random"},
                                 "proj", "ds")
    linha = json.loads(jsonl.strip())
    assert linha["md5"] == "01" * 16
    assert linha["boxes"] == [[0, 0.5, 0.5, 0.2, 0.2]]
    assert meta["stats"]["boxes_por_classe"]["classe_a"] == 1
    assert meta["stats"]["train"] == 1
    assert meta["gerado_por"].startswith("visionyard-api/")
    assert meta["roi"] is None                       # sem ROI por padrão


def test_manifesto_recusa_imagem_sem_md5():
    """`sample.md5` é anulável desde a 008 (linha pendente de backfill). O md5 é
    a identidade de conteúdo que o agente usa como chave de cache e nome do
    arquivo materializado — sem ele o item não pode entrar no manifesto. Antes
    estourava `TypeError` e o congelamento virava 500."""
    itens = [{"sample_id": "s1", "uri": "gs://b/1.jpg", "md5": b"\x01" * 16,
              "width": 100, "height": 80, "split": "train", "boxes": []},
             {"sample_id": "s2", "uri": "gs://b/2.jpg", "md5": None,
              "width": 100, "height": 80, "split": "train", "boxes": []}]
    with pytest.raises(HTTPException) as e:
        build_manifest(itens, ["classe_a"], ["gs://b/"], 1, "x",
                       {"method": "random"}, "proj", "ds")
    assert e.value.status_code == 409
    assert "md5" in e.value.detail and "gs://b/2.jpg" in e.value.detail
    # a mensagem não pode mandar reimportar: o dedup é por md5 e a linha nula
    # nunca casa, então reimportar duplica a imagem em vez de consertá-la
    assert "não resolve" in e.value.detail


def test_manifesto_nao_engole_erro_de_conversao_como_conflito():
    """Caixa com valor não numérico é dado corrompido, não conflito do usuário:
    tem de subir como erro interno, e não virar 409 com texto do Python."""
    itens = [{"sample_id": "s1", "uri": "gs://b/1.jpg", "md5": b"\x01" * 16,
              "width": 100, "height": 80, "split": "train",
              "boxes": [{"cls": 0, "cx": "x", "cy": .5, "w": .2, "h": .2}]}]
    with pytest.raises(ValueError):
        build_manifest(itens, ["classe_a"], ["gs://b/"], 1, "x",
                       {"method": "random"}, "proj", "ds")


def test_manifesto_carrega_roi():
    """A ROI do dataset viaja no meta.json para o agente aplicar o recorte. As
    caixas do items.jsonl seguem em coordenadas do quadro cheio (o agente é quem
    reprojeta) — mudar a ROI não reescreve caixas."""
    itens = [{"sample_id": "s1", "uri": "gs://b/1.jpg", "md5": b"\x01" * 16,
              "width": 100, "height": 80, "split": "train",
              "boxes": [{"cls": 0, "cx": .5, "cy": .5, "w": .2, "h": .2}]}]
    roi = {"x": 0.3, "y": 0.0, "w": 0.4, "h": 0.35}
    jsonl, meta = build_manifest(itens, ["classe_a"], ["gs://b/"], 1, "com roi",
                                 {"method": "random"}, "proj", "ds", roi=roi)
    assert meta["roi"] == roi
    assert json.loads(jsonl.strip())["boxes"] == [[0, 0.5, 0.5, 0.2, 0.2]]


# --------------------------------------------------------------------------- #
# Fluxo pela API
# --------------------------------------------------------------------------- #

async def _projeto_com_dados(admin, tmp_path):
    """Projeto com imagens e anotações importadas do Label Studio."""
    await admin.post("/v1/projects", json=PROJ)
    db_path, _ = _dump(tmp_path, n_tasks=6, annotate=5)
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/imports?dry_run=false",
                         json={"kind": "label_studio_sqlite",
                               "sqlite_path": str(db_path),
                               "image_root": str(tmp_path),
                               "image_base_uri": "gs://bucket/frames"})
    import tests.test_imports as ti

    from .test_imports import _aguarda
    ti.PROJ = PROJ                       # _aguarda usa o slug do módulo
    await _aguarda(admin, r.json()["job_id"])
    fontes = await admin.get(f"/v1/projects/{PROJ['slug']}/datasets")
    return fontes


async def _source_id(project_slug: str) -> str:
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        row = conn.execute(
            "SELECT ds.id FROM data_source ds JOIN project p"
            "  ON p.id = ds.project_id WHERE p.slug = %s ORDER BY ds.created_at"
            " LIMIT 1", (project_slug,)).fetchone()
    return str(row[0])


async def test_criar_dataset_e_adicionar_origem(admin, tmp_path):
    await _projeto_com_dados(admin, tmp_path)
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/datasets", json=DS)
    assert r.status_code == 201, r.text

    src = await _source_id(PROJ["slug"])
    r = await admin.put(
        f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}/sources/{src}")
    assert r.status_code == 204

    r = await admin.get(f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}")
    assert len(r.json()["origens"]) == 1
    assert r.json()["origens"][0]["imagens"] == 6


async def test_preview_conta_anotadas_e_pendentes(admin, tmp_path):
    await _projeto_com_dados(admin, tmp_path)
    await admin.post(f"/v1/projects/{PROJ['slug']}/datasets", json=DS)
    src = await _source_id(PROJ["slug"])
    await admin.put(
        f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}/sources/{src}")

    r = await admin.get(
        f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}/preview")
    d = r.json()
    assert d["imagens_nas_origens"] == 6
    assert d["anotadas"] == 5
    assert d["pendentes"] == 1
    assert d["entrariam"] == 5
    assert d["proxima_versao"] == 1


async def test_congelar_gera_manifesto(admin, tmp_path, monkeypatch):
    await _projeto_com_dados(admin, tmp_path)
    await admin.post(f"/v1/projects/{PROJ['slug']}/datasets", json=DS)
    src = await _source_id(PROJ["slug"])
    await admin.put(
        f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}/sources/{src}")

    r = await admin.post(
        f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}/freeze",
        json={"descricao": "base inicial", "val_ratio": 0.2})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["version"] == 1
    assert d["stats"]["imagens"] == 5

    out = Path(d["manifest_uri"])
    assert (out / "items.jsonl").is_file() and (out / "meta.json").is_file()
    linhas = (out / "items.jsonl").read_text().strip().split("\n")
    assert len(linhas) == 5
    meta = json.loads((out / "meta.json").read_text())
    # o hash no meta corresponde de fato ao arquivo de itens
    conteudo = (out / "items.jsonl").read_text()
    assert meta["items_sha256"] == hashlib.sha256(conteudo.encode()).hexdigest()
    assert meta["descricao"] == "base inicial"
    assert meta["origens"] == ["gs://bucket/frames"] or meta["origens"]

    for linha in linhas:
        item = json.loads(linha)
        assert item["split"] in ("train", "val")
        assert item["uri"].startswith("gs://")
        assert len(item["md5"]) == 32


async def test_roi_muda_identidade_da_versao(admin, tmp_path):
    """Congelar de novo só para aplicar uma ROI nova precisa gerar uma versão —
    a ROI muda o recorte materializado, então NÃO é 'conteúdo idêntico'."""
    await _projeto_com_dados(admin, tmp_path)
    await admin.post(f"/v1/projects/{PROJ['slug']}/datasets", json=DS)
    src = await _source_id(PROJ["slug"])
    await admin.put(
        f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}/sources/{src}")
    base = f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}"

    r = await admin.post(f"{base}/freeze", json={"descricao": "sem roi"})
    assert r.status_code == 201 and r.json()["version"] == 1

    # sem mudar nada → barrado (conteúdo idêntico, mesma ROI nula)
    r = await admin.post(f"{base}/freeze", json={"descricao": "igual"})
    assert r.status_code == 409

    # aplica ROI e congela de novo → nova versão, apesar do mesmo items.jsonl
    roi = {"x": 0.3, "y": 0.0, "w": 0.4, "h": 0.35}
    r = await admin.patch(base, json={"roi": roi})
    assert r.status_code == 200, r.text
    r = await admin.post(f"{base}/freeze", json={"descricao": "com roi"})
    assert r.status_code == 201 and r.json()["version"] == 2

    # a versão carrega a ROI carimbada (imutável)
    r = await admin.get(base)
    v2 = next(v for v in r.json()["versoes"] if v["version"] == 2)
    assert (v2["roi"]["x"], v2["roi"]["w"], v2["roi"]["h"]) == (0.3, 0.4, 0.35)

    # mesma ROI + mesmo conteúdo → barrado de novo
    r = await admin.post(f"{base}/freeze", json={"descricao": "de novo"})
    assert r.status_code == 409


async def test_congelar_duas_vezes_sem_mudanca_e_rejeitado(admin, tmp_path):
    """Não polui o histórico com versões idênticas."""
    await _projeto_com_dados(admin, tmp_path)
    await admin.post(f"/v1/projects/{PROJ['slug']}/datasets", json=DS)
    src = await _source_id(PROJ["slug"])
    await admin.put(
        f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}/sources/{src}")
    url = f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}/freeze"

    assert (await admin.post(url, json={"descricao": "v1"})).status_code == 201
    r = await admin.post(url, json={"descricao": "de novo"})
    assert r.status_code == 409
    assert "idêntico à v1" in r.json()["detail"]


def _zerar_md5_de_uma_anotada() -> None:
    """Reproduz o backfill pendente da 008: md5 zerado à mão numa imagem
    ANOTADA (é a que entra no congelamento).

    `sample` é imutável por trigger, daí o desligamento — que vai em
    try/finally porque o banco de teste é da SESSÃO inteira: uma falha no meio
    deixaria `sample` mutável para todos os testes seguintes, e uma regressão
    que faça UPDATE em `sample` passaria no CI em verde.
    """
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("ALTER TABLE sample DISABLE TRIGGER sample_immutable")
        try:
            conn.execute(
                "UPDATE sample SET md5 = NULL WHERE id ="
                " (SELECT sample_id FROM annotation LIMIT 1)")
        finally:
            conn.execute("ALTER TABLE sample ENABLE TRIGGER sample_immutable")


async def test_congelar_com_imagem_sem_md5_recusa(admin, tmp_path):
    """Base que passou pela 006 e teve o placeholder zerado tem sample com md5
    NULL. Congelar precisa recusar dizendo qual imagem trava — não estourar 500.
    """
    await _projeto_com_dados(admin, tmp_path)
    await admin.post(f"/v1/projects/{PROJ['slug']}/datasets", json=DS)
    src = await _source_id(PROJ["slug"])
    await admin.put(
        f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}/sources/{src}")

    _zerar_md5_de_uma_anotada()

    # a tela precisa saber ANTES de oferecer o botão: sem isto o preview conta a
    # imagem em `entrariam` e o congelamento dá 409 toda vez que o usuário clica
    r = await admin.get(
        f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}/preview")
    assert r.json()["sem_md5"] == 1

    r = await admin.post(
        f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}/freeze",
        json={"descricao": "com md5 pendente"})
    assert r.status_code == 409, r.text
    assert "md5" in r.json()["detail"]


async def test_dataset_sem_anotacao_nao_congela(admin, tmp_path):
    await admin.post("/v1/projects", json=PROJ)
    await admin.post(f"/v1/projects/{PROJ['slug']}/datasets", json=DS)
    r = await admin.post(
        f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}/freeze",
        json={"descricao": "vazio"})
    assert r.status_code == 409
    assert "nada a congelar" in r.json()["detail"]


async def test_remover_origem_preserva_versao_congelada(admin, tmp_path):
    """Remover é 'parar de incluir', nunca destruir."""
    await _projeto_com_dados(admin, tmp_path)
    await admin.post(f"/v1/projects/{PROJ['slug']}/datasets", json=DS)
    src = await _source_id(PROJ["slug"])
    base = f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}"
    await admin.put(f"{base}/sources/{src}")
    r = await admin.post(f"{base}/freeze", json={"descricao": "v1"})
    assert r.status_code == 201

    assert (await admin.delete(f"{base}/sources/{src}")).status_code == 204

    d = (await admin.get(base)).json()
    assert d["origens"] == []
    assert len(d["versoes"]) == 1              # a versão continua lá
    assert d["versoes"][0]["stats"]["imagens"] == 5
    # e os samples e anotações não foram apagados
    p = (await admin.get(f"/v1/projects/{PROJ['slug']}")).json()
    assert p["counts"]["samples"] == 6
    assert p["counts"]["annotations"] == 5


async def test_diff_entre_versoes(admin, tmp_path):
    await _projeto_com_dados(admin, tmp_path)
    await admin.post(f"/v1/projects/{PROJ['slug']}/datasets", json=DS)
    src = await _source_id(PROJ["slug"])
    base = f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}"
    await admin.put(f"{base}/sources/{src}")
    await admin.post(f"{base}/freeze", json={"descricao": "v1"})

    # segunda origem, com imagens novas
    outro = tmp_path / "mais"
    outro.mkdir()
    db2, _ = _dump(outro, n_tasks=3, annotate=3)
    # desloca o tint pra gerar imagens de conteúdo distinto do primeiro lote
    from .test_imports import _png
    for i, f in enumerate(sorted((outro / "frames").glob("*.png"))):
        _png(f, tint=200 + i)
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/imports?dry_run=false",
                         json={"kind": "label_studio_sqlite",
                               "sqlite_path": str(db2),
                               "image_root": str(outro),
                               "image_base_uri": "gs://bucket/mais"})
    from .test_imports import _aguarda
    await _aguarda(admin, r.json()["job_id"])

    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT ds.id FROM data_source ds JOIN project p"
            "  ON p.id = ds.project_id WHERE p.slug = %s"
            " ORDER BY ds.created_at", (PROJ["slug"],)).fetchall()
    assert len(rows) == 2
    await admin.put(f"{base}/sources/{rows[1][0]}")

    r = await admin.post(f"{base}/freeze", json={"descricao": "mais imagens"})
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 2
    assert r.json()["stats"]["imagens"] == 8      # 5 + 3

    d = (await admin.get(f"{base}/versions/2/diff")).json()
    assert d["de"] == 1 and d["para"] == 2
    assert len(d["entraram"]) == 3
    assert d["sairam"] == []


async def test_annotator_nao_congela(admin, client, tmp_path):
    from .test_projects import novo_usuario
    await _projeto_com_dados(admin, tmp_path)
    await admin.post(f"/v1/projects/{PROJ['slug']}/datasets", json=DS)
    async with novo_usuario(admin, client, "ana@teste.com") as (ana, _):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "ana@teste.com", "role": "annotator"})
        r = await ana.post(
            f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}/freeze",
            json={"descricao": "nao devia"})
        assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Correções do code review
# --------------------------------------------------------------------------- #

async def test_imagem_em_duas_origens_pertence_as_duas(admin, tmp_path):
    """A mesma foto vinda de dois prefixos deve contar nas duas origens.

    Antes, `sample.source_id` era escalar: a foto ficava presa à origem de onde
    veio primeiro, e um dataset que referenciasse só a segunda resolvia zero
    imagens — o "0 img" que apareceu na primeira tela do produto.
    """
    await admin.post("/v1/projects", json=PROJ)
    db1, imgs1 = _dump(tmp_path, n_tasks=4, annotate=4)

    # segunda origem: MESMOS arquivos, outro dump (simula outro prefixo)
    outro = tmp_path / "copia"
    outro.mkdir()
    db2, imgs2 = _dump(outro, n_tasks=0, annotate=0)   # cria frames/ vazio
    for f in sorted(imgs1.glob("*.png")):
        (imgs2 / f.name).write_bytes(f.read_bytes())   # mesmos bytes
    import json as _json
    import sqlite3
    c = sqlite3.connect(db2)
    for i, f in enumerate(sorted((outro / "frames").glob("*.png"))):
        imagem = f"/data/local-files/?d=frames/{f.name}"
        c.execute("INSERT INTO task VALUES (?,1,?)",
                  (i + 1, _json.dumps({"image": imagem})))
    c.commit()
    c.close()

    import tests.test_imports as ti

    from .test_imports import _aguarda
    ti.PROJ = PROJ
    for caminho, raiz in ((db1, tmp_path), (db2, outro)):
        r = await admin.post(f"/v1/projects/{PROJ['slug']}/imports?dry_run=false",
                             json={"kind": "label_studio_sqlite",
                                   "sqlite_path": str(caminho),
                                   "image_root": str(raiz)})
        await _aguarda(admin, r.json()["job_id"])

    import psycopg
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        fontes = [str(r[0]) for r in conn.execute(
            "SELECT ds.id FROM data_source ds JOIN project p"
            "  ON p.id = ds.project_id WHERE p.slug = %s ORDER BY ds.created_at",
            (PROJ["slug"],)).fetchall()]
        n = conn.execute("SELECT count(*) FROM sample").fetchone()[0]
    assert len(fontes) == 2
    assert n == 4, "as imagens são as mesmas: deduplicadas em 4 samples"

    # a SEGUNDA origem sozinha precisa resolver as 4 imagens
    await admin.post(f"/v1/projects/{PROJ['slug']}/datasets", json=DS)
    base = f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}"
    await admin.put(f"{base}/sources/{fontes[1]}")

    d = (await admin.get(base)).json()
    assert d["origens"][0]["imagens"] == 4, "origem mostrava 0 imagens"
    p = (await admin.get(f"{base}/preview")).json()
    assert p["imagens_nas_origens"] == 4


async def test_congelar_em_paralelo_nao_corrompe(admin, tmp_path):
    """Dois congelamentos simultâneos: um vence, o outro recusa limpo.

    Antes, ambos calculavam a mesma versão e escreviam no mesmo diretório; o
    segundo sobrescrevia o manifesto do primeiro, e o banco ficava com um
    content_hash que não correspondia ao arquivo em disco.
    """
    import asyncio
    await _projeto_com_dados(admin, tmp_path)
    await admin.post(f"/v1/projects/{PROJ['slug']}/datasets", json=DS)
    src = await _source_id(PROJ["slug"])
    base = f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}"
    await admin.put(f"{base}/sources/{src}")

    r1, r2 = await asyncio.gather(
        admin.post(f"{base}/freeze", json={"descricao": "A", "seed": 1}),
        admin.post(f"{base}/freeze", json={"descricao": "B", "seed": 2}),
        return_exceptions=True)
    codigos = sorted(r.status_code for r in (r1, r2)
                     if not isinstance(r, Exception))
    assert codigos == [201, 409], f"esperava um sucesso e uma recusa: {codigos}"

    d = (await admin.get(base)).json()
    assert len(d["versoes"]) == 1

    # o hash gravado tem de bater com o arquivo publicado
    import hashlib
    v = d["versoes"][0]
    conteudo = (Path(v["manifest_uri"]) / "items.jsonl").read_text()
    meta = json.loads((Path(v["manifest_uri"]) / "meta.json").read_text())
    assert meta["items_sha256"] == hashlib.sha256(conteudo.encode()).hexdigest()


async def test_por_grupo_sem_a_chave_recusa(admin, tmp_path):
    """by_group sem a chave degradava para aleatório mentindo no manifesto."""
    await _projeto_com_dados(admin, tmp_path)
    await admin.post(f"/v1/projects/{PROJ['slug']}/datasets", json=DS)
    src = await _source_id(PROJ["slug"])
    base = f"/v1/projects/{PROJ['slug']}/datasets/{DS['slug']}"
    await admin.put(f"{base}/sources/{src}")
    r = await admin.post(f"{base}/freeze",
                         json={"descricao": "x", "split_method": "by_group"})
    assert r.status_code == 409
    assert "agrupamento" in r.json()["detail"]


def test_split_nunca_esvazia_treino():
    itens = [{"sample_id": "s1", "metadata": {}}]
    assign_splits(itens, {"method": "random", "seed": 1, "val_ratio": 0.2})
    assert itens[0]["split"] == "train", "único item foi todo para validação"

    dois = [{"sample_id": f"s{i}", "metadata": {}} for i in range(2)]
    assign_splits(dois, {"method": "random", "seed": 1, "val_ratio": 0.9})
    assert {i["split"] for i in dois} == {"train", "val"}


def test_servico_de_dataset_nunca_alcanca_o_pool_sozinho():
    """Item 15 do PLANO, conferido onde ele foi aplicado.

    `db.fetch_*`/`db.execute` sem `conn=` pegam uma conexão cada. Num módulo
    que faz leia-decida-escreva — o congelamento relê a versão sob lock
    consultivo justamente para decidir o que gravar —, uma chamada solta
    escaparia da transação sem que nada acusasse: o tipo aceita, o lint aceita
    e a suíte passa, porque o resultado só diverge sob concorrência.

    Quem abre a conexão são as funções públicas deste módulo; todo o resto a
    recebe.
    """
    import ast
    from pathlib import Path as _P

    fonte = (_P(__file__).resolve().parents[1]
             / "api" / "services" / "datasets.py").read_text()
    soltas = [
        f"linha {no.lineno}: db.{no.func.attr}(...)"
        for no in ast.walk(ast.parse(fonte))
        if isinstance(no, ast.Call)
        and isinstance(no.func, ast.Attribute)
        and isinstance(no.func.value, ast.Name)
        and no.func.value.id == "db"
        and no.func.attr in {"fetch_one", "fetch_all", "fetch_row", "execute"}
        and not any(k.arg == "conn" for k in no.keywords)
    ]
    assert not soltas, "sem conn=, cada uma pega a sua conexão: " + "; ".join(soltas)
