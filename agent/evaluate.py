"""Executor do job `evaluate`: mede um modelo contra uma versão de benchmark.

Materializa o benchmark inteiro como `val` e roda `model.val` — as caixas do
manifesto são a verdade, e a saída é mAP/precisão/recall naquele conjunto fixo.
Como o benchmark nunca entra em treino, o número é comparável entre versões:
é assim que se decide qual modelo é melhor.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from contracts import jobs

from .ambiente import ambiente
from .client import Client
from .galeria import CONF_MIN, classificar, rotulo_para_quadro_cheio
from .materialize import _reprojetar_boxes, _reprojetar_polys, materializar, roi_tupla

# predições por POST: o mesmo tamanho de lote do pré-tag
LOTE = 200


def _finita(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def executar(cli: Client, job: dict, work_dir: Path) -> None:
    from ultralytics import YOLO

    from . import gcs

    job_id = job["id"]
    p = jobs.Evaluate.de_payload(job["payload"])
    imgsz = p.imgsz
    seq = {"n": 0}

    def log(msg: str, level: str = "info") -> None:
        seq["n"] += 1
        cli.events(job_id, [{"seq": seq["n"], "level": level, "message": msg}])

    meta = cli.manifest_meta(job_id)
    # Mesmo critério do treino: tarefa desconhecida para aqui, em vez de
    # medir contra rótulos que este agente não soube materializar.
    tarefa = meta.get("tarefa", "detect")
    if tarefa not in ("detect", "segment"):
        raise RuntimeError(f"tarefa '{tarefa}' desconhecida — atualize o agente")
    classes = meta["classes"]
    roi = roi_tupla(meta.get("roi"))
    log(f"benchmark: {meta['stats']['imagens']} imagens, {len(classes)} classes"
        + (f" — recorte ROI {roi}" if roi else ""))

    raiz = work_dir / job_id
    items = raiz / "items.jsonl"
    items.parent.mkdir(parents=True, exist_ok=True)
    cli.download_items(job_id, str(items))
    materializar(items, classes, raiz / "ds", work_dir / "cache-imagens", log,
                 split_fixo="val", roi=roi,
                 baixar=lambda pares, prog: gcs.baixar_lote(
                     cli, job_id, pares, prog))

    # o artefato pode ser o peso (.pt) ou um build (.onnx/.engine): o formato
    # muda a latência, que é justamente o que se quer comparar.
    from pathlib import Path as _P
    artefato = raiz / ("model" + (_P(p.artifact_uri).suffix or ".pt"))
    gcs.baixar(cli, job_id, p.artifact_uri, artefato)
    log(f"artefato {p.rotulo} carregado — avaliando")
    cli.job_heartbeat(job_id, {"etapa": "val"})

    modelo = YOLO(str(artefato))
    res = modelo.val(data=str(raiz / "ds" / "data.yaml"), imgsz=imgsz,
                     split="val", verbose=False)

    bruto = {"mAP50-95": res.box.map, "mAP50": res.box.map50,
             "precision": res.box.mp, "recall": res.box.mr}
    # Modelo -seg mede também a máscara, que é o que importa em segmentação;
    # as chaves são as mesmas que o treino publica (`train_worker.KEYMAP`).
    seg = getattr(res, "seg", None)
    if tarefa == "segment" and seg is not None:
        bruto.update({"mask_mAP50-95": seg.map, "mask_mAP50": seg.map50,
                      "mask_precision": seg.mp, "mask_recall": seg.mr})
    # res.speed: latência POR IMAGEM, decomposta — pré, inferência e pós — em ms.
    # É o gráfico de comparação que o usuário quer (onde o tempo é gasto).
    vel = getattr(res, "speed", None) or {}
    for chave, nome in (("preprocess", "pre_ms"), ("inference", "inf_ms"),
                        ("postprocess", "pos_ms")):
        bruto[nome] = vel.get(chave)
    metrics = {k: round(v, 5) for k, v in
               ((k, _finita(v)) for k, v in bruto.items()) if v is not None}
    log("mAP50-95={} | pré={} inf={} pós={} ms".format(
        metrics.get("mAP50-95"), metrics.get("pre_ms"),
        metrics.get("inf_ms"), metrics.get("pos_ms")))
    cli.evaluation(job_id, metrics, environment=ambiente(artefato))
    # A galeria é complemento: o número já está gravado, e falhar aqui não
    # pode transformar uma medição concluída em job falho.
    try:
        n = publicar_predicoes(cli, job_id, modelo, raiz / "ds" / "images" / "val",
                               items, imgsz, roi)
        log(f"galeria: {n} imagens com predição gravada")
    except Exception as e:
        log(f"galeria não gravada: {e}", "warn")
    cli.finish(job_id, "succeeded", result={"metrics": metrics})


def _arredondar(rotulo: list) -> list:
    return [rotulo[0]] + [round(v, 5) for v in rotulo[1:]]


def publicar_predicoes(cli: Client, job_id: str, modelo, pasta: Path,
                       items: Path, imgsz: int, roi: tuple | None) -> int:
    """Prevê cada imagem do benchmark, casa com o gabarito e envia em lotes.

    Com ROI, a comparação acontece no espaço do recorte, contra o gabarito
    recortado pelas MESMAS funções que o `materializar` usou: é contra ele que
    o `val` mediu. Objeto fora do recorte não existe para a avaliação, e o que
    cruza a borda conta só a parte de dentro. Depois de comparar, o gabarito e
    a predição voltam ao quadro cheio, que é onde a tela desenha."""
    gabarito: dict = {}
    with open(items) as f:
        for linha in f:
            if linha.strip():
                it = json.loads(linha)
                polys = it.get("polys")
                rotulos = polys if polys is not None else it.get("boxes") or []
                if roi is not None:
                    rotulos = (_reprojetar_polys(rotulos, roi) if polys is not None
                               else _reprojetar_boxes(rotulos, roi))
                gabarito[it["md5"]] = rotulos
    lote: list = []
    enviadas = 0
    for res in modelo.predict(str(pasta), imgsz=imgsz, conf=CONF_MIN,
                              stream=True, verbose=False):
        md5 = Path(res.path).stem
        pred = []
        # por índice, e não zip(strict=): o agente roda em Python 3.8
        cls, conf = res.boxes.cls.tolist(), res.boxes.conf.tolist()
        xywhn = res.boxes.xywhn.tolist()
        for i in range(len(cls)):
            pred.append([int(cls[i]), float(conf[i]), *xywhn[i]])
        gt = gabarito.get(md5, [])
        erros = classificar(gt, pred)
        tela_gt = [_arredondar(rotulo_para_quadro_cheio(g, roi)) for g in gt]
        tela_pred = [[p[0], round(p[1], 4),
                      *_arredondar(rotulo_para_quadro_cheio([p[0], *p[2:]], roi))[1:]]
                     for p in pred]
        lote.append({"md5": md5, "gt": tela_gt, "pred": tela_pred, **erros})
        if len(lote) >= LOTE:
            enviadas += cli.evaluation_predictions(job_id, lote)["gravadas"]
            lote = []
    if lote:
        enviadas += cli.evaluation_predictions(job_id, lote)["gravadas"]
    return enviadas
