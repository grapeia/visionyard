"""Converte o manifesto (items.jsonl) no layout que o YOLO treina.

O manifesto é a fonte da verdade — imutável, com hash. Aqui ele vira uma árvore
de arquivos:

    <dataset>/images/train/<nome>.jpg   labels/train/<nome>.txt
    <dataset>/images/val/<nome>.jpg     labels/val/<nome>.txt
    <dataset>/data.yaml

As imagens vêm do GCS, cacheadas por md5: o mesmo frame em dois treinos baixa
uma vez só. É o md5 do manifesto (identidade de conteúdo), então cache errado é
impossível — bytes diferentes têm md5 diferente.

Quem baixa é quem chama (`baixar`), porque baixar precisa do job: a URL é
assinada pela API para aquele job. Aqui fica só a decisão do que falta.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable, List, Tuple  # noqa: UP035

# (pares (uri, destino), progresso(n baixadas)) → baixa todos ou levanta.
# Alias avaliado no import: o future não adia essa expressão no Python 3.8.
Baixador = Callable[[List[Tuple[str, Path]], Callable[[int], None]], None]  # noqa: UP006


def _cache_path(cache_dir: Path, md5: str, uri: str) -> Path:
    ext = Path(uri).suffix or ".jpg"
    return cache_dir / f"{md5}{ext}"


def roi_tupla(roi: dict | None) -> tuple | None:
    """meta['roi'] ({x,y,w,h} ou None) → tupla (x,y,w,h) ou None."""
    if not roi:
        return None
    return (float(roi["x"]), float(roi["y"]), float(roi["w"]), float(roi["h"]))


def _reprojetar_boxes(boxes: list, roi: tuple) -> list:
    """Caixas YOLO [cls,cx,cy,w,h] do quadro cheio → espaço do recorte ROI.

    Mantém a caixa se o CENTRO cai dentro da ROI (o objeto "pertence" ao
    recorte) e descarta as de fora — que são justamente os vizinhos idênticos
    que a ROI existe para excluir. A que fica é clipada à borda do recorte.
    """
    rx, ry, rw, rh = roi
    out = []
    for b in boxes:
        cls, cx, cy, w, h = int(b[0]), b[1], b[2], b[3], b[4]
        if not (rx <= cx <= rx + rw and ry <= cy <= ry + rh):
            continue
        # cantos no espaço 0..1 do recorte, clipados à borda
        x1 = min(max((cx - w / 2 - rx) / rw, 0.0), 1.0)
        y1 = min(max((cy - h / 2 - ry) / rh, 0.0), 1.0)
        x2 = min(max((cx + w / 2 - rx) / rw, 0.0), 1.0)
        y2 = min(max((cy + h / 2 - ry) / rh, 0.0), 1.0)
        nw, nh = x2 - x1, y2 - y1
        if nw <= 0 or nh <= 0:
            continue
        out.append([cls, (x1 + x2) / 2, (y1 + y2) / 2, nw, nh])
    return out


def _faltando(items_jsonl: Path, cache_dir: Path) -> tuple[list[tuple[str, Path]], int]:
    """O que não está no cache, uma vez por arquivo, e quantos itens já estão.

    Separado do laço que escreve a árvore para que a assinatura vá em lote:
    pedir uma URL por imagem seriam milhares de idas à API por treino."""
    faltam: dict[Path, str] = {}
    cacheadas = 0
    with open(items_jsonl) as f:
        for linha in f:
            linha = linha.strip()
            if not linha:
                continue
            it = json.loads(linha)
            caminho = _cache_path(cache_dir, it["md5"], it["uri"])
            if caminho.is_file():
                cacheadas += 1
            elif caminho not in faltam:
                faltam[caminho] = it["uri"]
    return [(uri, caminho) for caminho, uri in faltam.items()], cacheadas


def materializar(items_jsonl: Path, classes: list[str], destino: Path,
                 cache_dir: Path, log, split_fixo: str | None = None,
                 roi: tuple | None = None, *, baixar: Baixador) -> dict:
    """Escreve a árvore YOLO e devolve contagens. `log(msg)` reporta progresso.

    `split_fixo` força todas as imagens para um split (a avaliação usa 'val': o
    conjunto inteiro do benchmark é medido, sem treino).

    `roi` (x,y,w,h em frações 0–1) recorta cada imagem à região de interesse e
    reprojeta as caixas para o recorte — o MESMO recorte precisa ser aplicado na
    inferência, senão produção mede num espaço diferente do treino. Com recorte
    não dá para hardlinkar (os bytes mudam): a imagem é reencodada."""
    for split in ("train", "val"):
        (destino / "images" / split).mkdir(parents=True, exist_ok=True)
        (destino / "labels" / split).mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    pares, cacheadas = _faltando(items_jsonl, cache_dir)
    baixadas = len(pares)
    if pares:
        log(f"baixando {baixadas} imagens ({cacheadas} já no cache)")
        baixar(pares, lambda feitas: log(f"baixadas {feitas}/{baixadas}"))

    n = {"train": 0, "val": 0}
    with open(items_jsonl) as f:
        for i, linha in enumerate(f):
            linha = linha.strip()
            if not linha:
                continue
            it = json.loads(linha)
            split = split_fixo or it["split"]
            md5 = it["md5"]
            uri = it["uri"]
            nome = f"{md5}"
            img_cache = _cache_path(cache_dir, md5, uri)

            boxes = it.get("boxes", [])
            if roi is not None:
                # recorta e reencoda: bytes diferentes não hardlinkam. As caixas
                # vão para o espaço do recorte (as fora da ROI somem).
                destino_img = destino / "images" / split / (nome + ".jpg")
                if not destino_img.exists():
                    from PIL import Image
                    with Image.open(img_cache) as arq:
                        im = arq.convert("RGB")
                        W, H = im.size
                        x1 = min(max(round(roi[0] * W), 0), W)
                        y1 = min(max(round(roi[1] * H), 0), H)
                        x2 = min(max(round((roi[0] + roi[2]) * W), 0), W)
                        y2 = min(max(round((roi[1] + roi[3]) * H), 0), H)
                        im.crop((x1, y1, x2, y2)).save(
                            destino_img, "JPEG", quality=95)
                boxes = _reprojetar_boxes(boxes, roi)
            else:
                destino_img = destino / "images" / split / \
                    (nome + img_cache.suffix)
                if not destino_img.exists():
                    # hardlink quando possível (mesmo FS): não duplica bytes
                    try:
                        destino_img.hardlink_to(img_cache)
                    except (OSError, AttributeError):
                        shutil.copy2(img_cache, destino_img)

            rotulo = destino / "labels" / split / (nome + ".txt")
            linhas = [f"{int(b[0])} {b[1]:.6f} {b[2]:.6f} {b[3]:.6f} {b[4]:.6f}"
                      for b in boxes]
            rotulo.write_text("\n".join(linhas) + ("\n" if linhas else ""))
            n[split] += 1
            if (i + 1) % 200 == 0:
                log(f"materializadas {i + 1} imagens")

    data_yaml = destino / "data.yaml"
    data_yaml.write_text(
        f"path: {destino.resolve()}\n"
        f"train: images/train\n"
        f"val: images/val\n"
        f"nc: {len(classes)}\n"
        f"names: {json.dumps(classes, ensure_ascii=False)}\n")
    log(f"pronto: {n['train']} treino, {n['val']} val "
        f"({baixadas} baixadas, {cacheadas} do cache)")
    return {"train": n["train"], "val": n["val"], "data_yaml": str(data_yaml)}
