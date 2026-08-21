"""Executor do job `evaluate`: mede um modelo contra uma versão de benchmark.

Materializa o benchmark inteiro como `val` e roda `model.val` — as caixas do
manifesto são a verdade, e a saída é mAP/precisão/recall naquele conjunto fixo.
Como o benchmark nunca entra em treino, o número é comparável entre versões:
é assim que se decide qual modelo é melhor.
"""
from __future__ import annotations

import math
from pathlib import Path

from contracts import jobs

from .client import Client
from .materialize import materializar, roi_tupla


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
    classes = meta["classes"]
    roi = roi_tupla(meta.get("roi"))
    log(f"benchmark: {meta['stats']['imagens']} imagens, {len(classes)} classes"
        + (f" — recorte ROI {roi}" if roi else ""))

    raiz = work_dir / job_id
    items = raiz / "items.jsonl"
    items.parent.mkdir(parents=True, exist_ok=True)
    cli.download_items(job_id, str(items))
    materializar(items, classes, raiz / "ds", work_dir / "cache-imagens", log,
                 split_fixo="val", roi=roi)

    # o artefato pode ser o peso (.pt) ou um build (.onnx/.engine): o formato
    # muda a latência, que é justamente o que se quer comparar.
    from pathlib import Path as _P
    artefato = raiz / ("model" + (_P(p.artifact_uri).suffix or ".pt"))
    gcs.baixar(p.artifact_uri, artefato)
    log(f"artefato {p.rotulo} carregado — avaliando")
    cli.job_heartbeat(job_id, {"etapa": "val"})

    modelo = YOLO(str(artefato))
    res = modelo.val(data=str(raiz / "ds" / "data.yaml"), imgsz=imgsz,
                     split="val", verbose=False)

    bruto = {"mAP50-95": res.box.map, "mAP50": res.box.map50,
             "precision": res.box.mp, "recall": res.box.mr}
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
    cli.evaluation(job_id, metrics)
    cli.finish(job_id, "succeeded", result={"metrics": metrics})
