"""Do resultado da Ultralytics para a predição que a API grava — sem modelo."""
from __future__ import annotations

from types import SimpleNamespace as NS

from agent import prelabel


class _T(list):
    """Imita o tensor de 1 linha: `t[0].tolist()` e `float(t[0])`."""
    def tolist(self):
        return list(self)


def _caixa(cls, conf, xywhn=(.5, .5, .2, .2)):
    return NS(cls=[cls], conf=[conf], xywhn=[_T(xywhn)])


def test_deteccao_devolve_caixa():
    res = NS(boxes=[_caixa(1, .91234)], masks=None)
    assert prelabel.previsoes(res) == [
        {"cls": 1, "cx": .5, "cy": .5, "w": .2, "h": .2, "conf": .9123}]


def test_segmentacao_devolve_contorno_simplificado():
    """A máscara vem como contorno de centenas de pontos (um por pixel de
    borda). Simplificado, porque é isso que a pessoa vai ajustar vértice a
    vértice; e sem os colineares, que não mudam a forma."""
    lado = [(i / 100, .1) for i in range(10, 91)]          # 81 pontos numa reta
    contorno = [*lado, (.9, .9), (.1, .9)]
    res = NS(boxes=[_caixa(0, .8)], masks=NS(xyn=[contorno]))
    [p] = prelabel.previsoes(res)
    assert p["cls"] == 0 and p["conf"] == .8
    assert p["pts"] == [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]


def test_segmentacao_descarta_contorno_sem_area():
    res = NS(boxes=[_caixa(0, .8), _caixa(0, .7)],
             masks=NS(xyn=[[(.1, .1), (.2, .2)], []]))
    assert prelabel.previsoes(res) == []
