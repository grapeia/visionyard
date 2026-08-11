"""Materialização do manifesto no layout YOLO — sem GPU nem GCS.

O download do GCS é substituído por uma função que escreve bytes de mentira; o
que importa aqui é a árvore de arquivos, o formato do rótulo e o cache por md5.
"""
from __future__ import annotations

import json
from pathlib import Path

from agent import materialize


def _manifesto(tmp: Path, linhas: list[dict]) -> Path:
    p = tmp / "items.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in linhas) + "\n")
    return p


def test_materializar_gera_arvore_yolo(tmp_path, monkeypatch):
    baixados = []

    def fake_baixar(uri, destino):
        baixados.append(uri)
        Path(destino).write_bytes(b"imagem-falsa")

    monkeypatch.setattr(materialize.gcs, "baixar", fake_baixar)

    itens = [
        {"uri": "gs://b/a.jpg", "md5": "a" * 32, "split": "train",
         "boxes": [[0, 0.5, 0.5, 0.2, 0.3]]},
        {"uri": "gs://b/c.jpg", "md5": "c" * 32, "split": "val",
         "boxes": []},                                   # negativo: sem caixa
    ]
    items = _manifesto(tmp_path, itens)
    destino = tmp_path / "ds"
    conta = materialize.materializar(items, ["classe_a", "classe_b"],
                                     destino, tmp_path / "cache", lambda *_: None)

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


def test_roi_recorta_imagem_e_reprojeta_caixas(tmp_path, monkeypatch):
    """Com ROI: a imagem é recortada, a caixa central é reprojetada para o
    espaço do recorte, e o vizinho (centro fora da ROI) é descartado."""
    from PIL import Image

    def fake_baixar(uri, destino):
        Image.new("RGB", (100, 100), (120, 120, 120)).save(destino, "JPEG")

    monkeypatch.setattr(materialize.gcs, "baixar", fake_baixar)

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
                             lambda *_: None, roi=roi)

    img = Image.open(destino / "images" / "train" / f"{'a' * 32}.jpg")
    assert img.size == (50, 50), "imagem não foi recortada para a ROI"
    rot = (destino / "labels" / "train" / f"{'a' * 32}.txt").read_text().strip()
    # 1 linha só (vizinho descartado), caixa central centrada no recorte
    assert rot == "0 0.500000 0.500000 0.400000 0.400000"


def test_cache_por_md5_nao_baixa_de_novo(tmp_path, monkeypatch):
    """Mesmo md5 em dois itens (a foto entra em treino e reaparece): baixa uma
    vez só. É o md5 do conteúdo, então cache trocado é impossível."""
    n = {"baixas": 0}

    def fake_baixar(uri, destino):
        n["baixas"] += 1
        Path(destino).write_bytes(b"x")

    monkeypatch.setattr(materialize.gcs, "baixar", fake_baixar)

    itens = [{"uri": "gs://b/a.jpg", "md5": "a" * 32, "split": "train", "boxes": []},
             {"uri": "gs://b/a.jpg", "md5": "a" * 32, "split": "val", "boxes": []}]
    items = _manifesto(tmp_path, itens)
    materialize.materializar(items, ["c0"], tmp_path / "ds",
                             tmp_path / "cache", lambda *_: None)
    assert n["baixas"] == 1, "baixou a mesma imagem duas vezes"
