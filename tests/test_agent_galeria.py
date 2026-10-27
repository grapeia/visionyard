"""Classificação do erro por imagem que alimenta a galeria da avaliação."""
from __future__ import annotations

from agent.galeria import caixa_de, classificar, para_quadro_cheio

OBJ = [0, 0.5, 0.5, 0.2, 0.2]


def test_acerto_nao_conta_erro():
    assert classificar([OBJ], [[0, 0.9, 0.5, 0.5, 0.2, 0.2]]) == \
        {"fp": 0, "fn": 0, "swapped": 0}


def test_objeto_sem_predicao_e_falso_negativo():
    assert classificar([OBJ], []) == {"fp": 0, "fn": 1, "swapped": 0}


def test_predicao_no_vazio_e_falso_positivo():
    assert classificar([], [[0, 0.9, 0.1, 0.1, 0.1, 0.1]]) == \
        {"fp": 1, "fn": 0, "swapped": 0}


def test_classe_errada_no_objeto_certo_e_troca_e_nao_fp_mais_fn():
    assert classificar([OBJ], [[1, 0.9, 0.5, 0.5, 0.2, 0.2]]) == \
        {"fp": 0, "fn": 0, "swapped": 1}


def test_duas_predicoes_no_mesmo_objeto_a_menos_confiante_sobra():
    r = classificar([OBJ], [[0, 0.6, 0.5, 0.5, 0.2, 0.2],
                            [0, 0.9, 0.51, 0.5, 0.2, 0.2]])
    assert r == {"fp": 1, "fn": 0, "swapped": 0}


def test_objetos_sobrepostos_a_classe_certa_tem_preferencia():
    """A predição da classe 0 sobre o objeto 0 não é troca só porque o objeto
    1, sobreposto, tem IoU maior com ela."""
    outro = [1, 0.52, 0.5, 0.2, 0.2]
    proprio = [0, 0.56, 0.5, 0.2, 0.2]
    assert classificar([outro, proprio], [[0, 0.9, 0.52, 0.5, 0.2, 0.2]]) == \
        {"fp": 0, "fn": 1, "swapped": 0}


def test_iou_abaixo_do_limite_nao_casa():
    # deslocada meia largura: IoU 1/3
    assert classificar([OBJ], [[0, 0.9, 0.6, 0.5, 0.2, 0.2]]) == \
        {"fp": 1, "fn": 1, "swapped": 0}


def test_poligono_do_gabarito_casa_pelo_retangulo_que_o_envolve():
    poligono = [0, 0.4, 0.4, 0.6, 0.4, 0.6, 0.6, 0.4, 0.6]
    assert caixa_de(poligono) == (0.5, 0.5, 0.19999999999999996, 0.19999999999999996)
    assert classificar([poligono], [[0, 0.9, 0.5, 0.5, 0.2, 0.2]])["fp"] == 0


def test_predicao_no_recorte_volta_ao_quadro_cheio():
    # ROI na metade direita: o centro do recorte é 3/4 do quadro
    assert para_quadro_cheio((0.5, 0.5, 0.2, 0.2), (0.5, 0.0, 0.5, 1.0)) == \
        (0.75, 0.5, 0.1, 0.2)


# --------------------------------------------------------------------------- #
# publicar_predicoes: o gabarito é o que o `val` mediu, recortado como ele
# --------------------------------------------------------------------------- #

class _Caixas:
    def __init__(self, linhas):     # linhas: [cls, conf, cx, cy, w, h]
        self.cls = _T([linha[0] for linha in linhas])
        self.conf = _T([linha[1] for linha in linhas])
        self.xywhn = _T([linha[2:] for linha in linhas])


class _T(list):
    def tolist(self):
        return list(self)


class _Res:
    def __init__(self, path, linhas):
        self.path, self.boxes = path, _Caixas(linhas)


class _Modelo:
    def __init__(self, por_imagem):
        self.por_imagem = por_imagem

    def predict(self, pasta, **_):
        return [_Res(f"{pasta}/{md5}.jpg", linhas)
                for md5, linhas in self.por_imagem.items()]


class _Cliente:
    def __init__(self):
        self.lotes = []

    def evaluation_predictions(self, job_id, lote):
        self.lotes.append(lote)
        return {"gravadas": len(lote)}


def _publicar(tmp_path, gt, pred, roi, tarefa_seg=False):
    import json

    from agent.evaluate import publicar_predicoes
    items = tmp_path / "items.jsonl"
    linha = {"md5": "a" * 32, "uri": "gs://b/a.jpg", "split": "val"}
    linha.update({"polys": gt, "boxes": None} if tarefa_seg else {"boxes": gt})
    items.write_text(json.dumps(linha) + "\n")
    cli = _Cliente()
    publicar_predicoes(cli, "job", _Modelo({"a" * 32: pred}), tmp_path, items,
                       640, roi)
    return cli.lotes[0][0]


ROI_DIREITA = (0.5, 0.0, 0.5, 1.0)


def test_objeto_fora_da_roi_nao_e_falso_negativo(tmp_path):
    """O `val` não viu o objeto fora do recorte; a galeria também não pode."""
    r = _publicar(tmp_path, [[0, 0.2, 0.5, 0.1, 0.1]], [], ROI_DIREITA)
    assert (r["fn"], r["gt"]) == (0, [])


def test_objeto_cruzando_a_borda_casa_com_a_parte_de_dentro(tmp_path):
    # caixa de x 0.1..0.9: o recorte guarda 0.5..0.9, que no recorte é
    # cx 0.4, w 0.8; a predição perfeita do recorte não pode virar FP + FN
    r = _publicar(tmp_path, [[0, 0.5, 0.5, 0.8, 0.2]],
                  [[0, 0.9, 0.4, 0.5, 0.8, 0.2]], ROI_DIREITA)
    assert (r["fp"], r["fn"], r["swapped"]) == (0, 0, 0)
    # e as duas voltam ao quadro cheio para a tela desenhar
    assert r["gt"] == [[0, 0.7, 0.5, 0.4, 0.2]]
    assert r["pred"] == [[0, 0.9, 0.7, 0.5, 0.4, 0.2]]


def test_poligono_e_cortado_na_roi_e_volta_ao_quadro_cheio(tmp_path):
    quadrado = [0, 0.4, 0.4, 0.8, 0.4, 0.8, 0.6, 0.4, 0.6]
    r = _publicar(tmp_path, [quadrado], [[0, 0.9, 0.3, 0.5, 0.6, 0.2]],
                  ROI_DIREITA, tarefa_seg=True)
    assert (r["fp"], r["fn"]) == (0, 0)
    xs = r["gt"][0][1::2]
    assert min(xs) == 0.5 and max(xs) == 0.8


def test_sem_roi_o_gabarito_vai_como_esta(tmp_path):
    r = _publicar(tmp_path, [[0, 0.2, 0.5, 0.1, 0.1]], [], None)
    assert (r["fn"], r["gt"]) == (1, [[0, 0.2, 0.5, 0.1, 0.1]])
