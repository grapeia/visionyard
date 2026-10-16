"""Materialização do manifesto no layout YOLO — sem GPU nem GCS.

O download é substituído por uma função que escreve bytes de mentira; o que
importa aqui é a árvore de arquivos, o formato do rótulo e o cache por md5.
"""
from __future__ import annotations

import json
from pathlib import Path

from agent import materialize


def _baixador(baixar_um):
    """O `baixar` em lote que o executor passa, sobre um download falso."""
    def baixar(pares, progresso):
        for uri, destino in pares:
            baixar_um(uri, destino)
        progresso(len(pares))
    return baixar


def _manifesto(tmp: Path, linhas: list[dict]) -> Path:
    p = tmp / "items.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in linhas) + "\n")
    return p


def test_materializar_gera_arvore_yolo(tmp_path):
    baixados = []

    def fake_baixar(uri, destino):
        baixados.append(uri)
        Path(destino).write_bytes(b"imagem-falsa")

    itens = [
        {"uri": "gs://b/a.jpg", "md5": "a" * 32, "split": "train",
         "boxes": [[0, 0.5, 0.5, 0.2, 0.3]]},
        {"uri": "gs://b/c.jpg", "md5": "c" * 32, "split": "val",
         "boxes": []},                                   # negativo: sem caixa
    ]
    items = _manifesto(tmp_path, itens)
    destino = tmp_path / "ds"
    conta = materialize.materializar(items, ["classe_a", "classe_b"],
                                     destino, tmp_path / "cache", lambda *_: None,
                                     baixar=_baixador(fake_baixar))

    assert conta["train"] == 1 and conta["val"] == 1
    # imagens no lugar certo
    assert (destino / "images" / "train" / f"{'a' * 32}.jpg").is_file()
    assert (destino / "images" / "val" / f"{'c' * 32}.jpg").is_file()
    # rótulo no formato YOLO
    rot = (destino / "labels" / "train" / f"{'a' * 32}.txt").read_text().strip()
    assert rot == "0 0.500000 0.500000 0.200000 0.300000"
    # exemplo negativo → rótulo vazio (não ausente: YOLO trata como sem objeto)
    assert (destino / "labels" / "val" / f"{'c' * 32}.txt").read_text() == ""
    # data.yaml com as classes na ordem
    dy = (destino / "data.yaml").read_text()
    assert '"classe_a", "classe_b"' in dy
    assert "nc: 2" in dy


def test_roi_recorta_imagem_e_reprojeta_caixas(tmp_path):
    """Com ROI: a imagem é recortada, a caixa central é reprojetada para o
    espaço do recorte, e o vizinho (centro fora da ROI) é descartado."""
    from PIL import Image

    def fake_baixar(uri, destino):
        Image.new("RGB", (100, 100), (120, 120, 120)).save(destino, "JPEG")

    itens = [{
        "uri": "gs://b/a.jpg", "md5": "a" * 32, "split": "train",
        "boxes": [
            [0, 0.5, 0.25, 0.2, 0.2],    # central: centro dentro da ROI
            [0, 0.05, 0.25, 0.1, 0.1],   # vizinho: centro fora → some
        ],
    }]
    items = _manifesto(tmp_path, itens)
    destino = tmp_path / "ds"
    roi = (0.25, 0.0, 0.5, 0.5)          # crop x[25,75] y[0,50] → 50×50
    materialize.materializar(items, ["c0"], destino, tmp_path / "cache",
                             lambda *_: None, roi=roi,
                             baixar=_baixador(fake_baixar))

    img = Image.open(destino / "images" / "train" / f"{'a' * 32}.jpg")
    assert img.size == (50, 50), "imagem não foi recortada para a ROI"
    rot = (destino / "labels" / "train" / f"{'a' * 32}.txt").read_text().strip()
    # 1 linha só (vizinho descartado), caixa central centrada no recorte
    assert rot == "0 0.500000 0.500000 0.400000 0.400000"


def test_cache_por_md5_nao_baixa_de_novo(tmp_path):
    """Mesmo md5 em dois itens (a foto entra em treino e reaparece): baixa uma
    vez só. É o md5 do conteúdo, então cache trocado é impossível."""
    n = {"baixas": 0}

    def fake_baixar(uri, destino):
        n["baixas"] += 1
        Path(destino).write_bytes(b"x")

    itens = [{"uri": "gs://b/a.jpg", "md5": "a" * 32, "split": "train", "boxes": []},
             {"uri": "gs://b/a.jpg", "md5": "a" * 32, "split": "val", "boxes": []}]
    items = _manifesto(tmp_path, itens)
    materialize.materializar(items, ["c0"], tmp_path / "ds",
                             tmp_path / "cache", lambda *_: None,
                             baixar=_baixador(fake_baixar))
    assert n["baixas"] == 1, "baixou a mesma imagem duas vezes"


def _baixa_falso(uri, destino):
    Path(destino).write_bytes(b"imagem-falsa")


def test_materializar_escreve_poligono_no_formato_yolo_seg(tmp_path):
    itens = [{"uri": "gs://b/a.jpg", "md5": "a" * 32, "split": "train",
              "polys": [[1, .1, .1, .9, .1, .5, .9]]}]
    destino = tmp_path / "ds"
    materialize.materializar(_manifesto(tmp_path, itens), ["banda", "hematoma"],
                             destino, tmp_path / "cache", lambda *_: None,
                             baixar=_baixador(_baixa_falso))
    rot = (destino / "labels" / "train" / f"{'a' * 32}.txt").read_text().strip()
    assert rot == ("1 0.100000 0.100000 0.900000 0.100000 "
                   "0.500000 0.900000")


def test_poligono_e_recortado_pela_roi():
    """O que passa da ROI é cortado na borda dela (não descartado: a banda
    inteira raramente cabe no recorte), e o resto vai para o espaço 0..1 do
    recorte. Polígono todo fora some."""
    roi = (0.5, 0.0, 0.5, 1.0)                 # metade direita
    quadrado = [0, .25, .25, .75, .25, .75, .75, .25, .75]
    fora = [0, .1, .1, .2, .1, .2, .2]
    out = materialize._reprojetar_polys([quadrado, fora], roi)
    assert len(out) == 1
    _, *xy = out[0]
    pts = sorted(zip(xy[0::2], xy[1::2], strict=True))
    assert [(round(x, 6), round(y, 6)) for x, y in pts] == [
        (0.0, 0.25), (0.0, 0.75), (0.5, 0.25), (0.5, 0.75)]
