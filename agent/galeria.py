"""Erro por imagem do benchmark, para a galeria da avaliação.

O mAP diz quanto o modelo erra; a galeria mostra onde. Para cada imagem o
agente casa as caixas previstas com as do gabarito e conta três tipos de erro:

  * falso positivo — predição que não cobre objeto nenhum;
  * falso negativo — objeto que nenhuma predição cobriu;
  * classe trocada — cobriu o objeto certo com a classe errada.

O casamento é guloso por confiança, como no cálculo de mAP: a predição mais
confiante escolhe primeiro o objeto de maior IoU ainda livre. Classe trocada
conta à parte, e não como um FP mais um FN, porque são erros de natureza
diferente — um pede mais exemplo da classe, o outro mais exemplo do objeto.

Puro e sem dependência: roda no agente em Python 3.8, e o teste não precisa
de GPU.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Tuple  # noqa: UP035

IOU_MIN = 0.5
# Confiança mínima da predição que entra na galeria. O `val` mede com 0.001 —
# certo para a curva do mAP, mas na tela seriam dezenas de caixas de ruído por
# imagem. É o mesmo padrão do pré-tag.
CONF_MIN = 0.25

# Alias avaliado no import: `tuple[...]` não existe no Python 3.8.
Caixa = Tuple[float, float, float, float]  # noqa: UP006 — cx, cy, w, h normalizados


def caixa_de(rotulo: Sequence[float]) -> Caixa:
    """Linha do manifesto → caixa. `[cls, cx, cy, w, h]` já é caixa; o polígono
    `[cls, x1, y1, x2, y2, ...]` vira o retângulo que o envolve."""
    v = [float(x) for x in rotulo[1:]]
    if len(v) == 4:
        return (v[0], v[1], v[2], v[3])
    xs, ys = v[0::2], v[1::2]
    x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
    return ((x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1)


def iou(a: Caixa, b: Caixa) -> float:
    ax1, ay1, ax2, ay2 = (a[0] - a[2] / 2, a[1] - a[3] / 2,
                          a[0] + a[2] / 2, a[1] + a[3] / 2)
    bx1, by1, bx2, by2 = (b[0] - b[2] / 2, b[1] - b[3] / 2,
                          b[0] + b[2] / 2, b[1] + b[3] / 2)
    iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    uniao = a[2] * a[3] + b[2] * b[3] - inter
    return inter / uniao if uniao > 0 else 0.0


def _melhor(caixa: Caixa, candidatos, gt) -> int | None:
    melhor, melhor_iou = None, IOU_MIN
    for i in candidatos:
        v = iou(caixa, gt[i][1])
        if v >= melhor_iou:
            melhor, melhor_iou = i, v
    return melhor


def classificar(gabarito: Sequence[Sequence[float]],
                predicoes: Sequence[Sequence[float]]) -> dict:
    """`gabarito`: linhas do manifesto (`[cls, ...]`). `predicoes`:
    `[cls, conf, cx, cy, w, h]`. Devolve as contagens `fp`, `fn`, `swapped`."""
    gt = [(int(g[0]), caixa_de(g)) for g in gabarito]
    livres = set(range(len(gt)))
    fp = swapped = 0
    for p in sorted(predicoes, key=lambda p: -float(p[1])):
        caixa = (float(p[2]), float(p[3]), float(p[4]), float(p[5]))
        # A mesma classe tem preferência: com dois objetos sobrepostos, a
        # predição certa sobre um não vira "troca" por ter IoU maior com o outro.
        melhor = _melhor(caixa, [i for i in livres if gt[i][0] == int(p[0])], gt)
        if melhor is None:
            melhor = _melhor(caixa, livres, gt)
        if melhor is None:
            fp += 1
            continue
        livres.discard(melhor)
        if gt[melhor][0] != int(p[0]):
            swapped += 1
    return {"fp": fp, "fn": len(livres), "swapped": swapped}


def para_quadro_cheio(caixa: Caixa, roi: Caixa | None) -> Caixa:
    """Caixa no espaço do recorte da ROI → coordenadas do quadro cheio, que é
    onde o manifesto e a tela desenham."""
    if roi is None:
        return caixa
    rx, ry, rw, rh = roi
    return (rx + caixa[0] * rw, ry + caixa[1] * rh, caixa[2] * rw, caixa[3] * rh)


def rotulo_para_quadro_cheio(rotulo: Sequence[float],
                             roi: Caixa | None) -> list:
    """Linha do manifesto (caixa ou polígono) do recorte → quadro cheio."""
    cls, v = int(rotulo[0]), [float(x) for x in rotulo[1:]]
    if roi is None:
        return [cls, *v]
    if len(v) == 4:
        return [cls, *para_quadro_cheio((v[0], v[1], v[2], v[3]), roi)]
    rx, ry, rw, rh = roi
    return [cls] + [rx + x * rw if i % 2 == 0 else ry + x * rh
                    for i, x in enumerate(v)]
