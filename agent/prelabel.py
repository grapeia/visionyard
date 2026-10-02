"""Executor do job `prelabel`: roda um modelo sobre as imagens do escopo e
devolve as caixas previstas.

Leve perto do treino: baixa o peso uma vez, prevê imagem a imagem, envia em
lotes. As imagens vêm do GCS por URL que a API assina, um lote por vez;
nenhuma anotação humana é tocada — a decisão de o que sobrescrever é da API.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from contracts import jobs

from . import gcs
from .client import Client

LOTE = 32          # envia predições a cada N imagens, para a tela ver progresso


def executar(cli: Client, job: dict, work_dir: Path) -> None:
    from ultralytics import YOLO

    job_id = job["id"]
    p = jobs.Prelabel.de_payload(job["payload"])
    seq = {"n": 0}

    def log(msg: str, level: str = "info") -> None:
        seq["n"] += 1
        cli.events(job_id, [{"seq": seq["n"], "level": level, "message": msg}])

    tinfo = cli.targets(job_id)
    alvos = tinfo["alvos"]
    conf = tinfo.get("conf", 0.25)
    log(f"{len(alvos)} imagens a pré-taguear, conf={conf}")
    if not alvos:
        cli.finish(job_id, "succeeded", result={"pretags": 0, "imagens": 0})
        return

    # peso: baixa do bucket para um cache local por sha do URI
    raiz = work_dir / job_id
    raiz.mkdir(parents=True, exist_ok=True)
    peso = raiz / "model.pt"
    gcs.baixar(cli, job_id, p.weights_uri, peso)
    modelo = YOLO(str(peso))
    log("modelo carregado")

    cache = work_dir / "cache-imagens"
    cache.mkdir(parents=True, exist_ok=True)
    pendentes: list[dict] = []
    total_boxes = 0
    cancelou = False

    def enviar():
        if pendentes:
            cli.prelabel(job_id, list(pendentes))
            pendentes.clear()

    def local(uri: str) -> Path:
        # cache por hash do URI COMPLETO, não pelo basename: dois prefixos com
        # o mesmo nome de arquivo (camA/frame1.jpg vs camB/frame1.jpg) colidiam
        # e o modelo previa na imagem errada, em silêncio.
        return cache / (hashlib.sha1(uri.encode()).hexdigest() + Path(uri).suffix)

    for i, alvo in enumerate(alvos):
        if i % LOTE == 0:
            # baixa o bloco que vem antes de prevê-lo: uma assinatura por
            # bloco, e a URL é usada logo, longe de expirar.
            faltam = [(a["uri"], local(a["uri"])) for a in alvos[i:i + LOTE]
                      if not local(a["uri"]).is_file()]
            if faltam:
                gcs.baixar_lote(cli, job_id, faltam)
        img = local(alvo["uri"])
        res = modelo.predict(str(img), conf=conf, verbose=False)[0]
        boxes = []
        for b in res.boxes:
            cx, cy, w, h = b.xywhn[0].tolist()   # normalizado 0..1, centro
            boxes.append({"cls": int(b.cls[0]), "cx": cx, "cy": cy,
                          "w": w, "h": h, "conf": round(float(b.conf[0]), 4)})
        total_boxes += len(boxes)
        pendentes.append({"sample_id": alvo["sample_id"], "boxes": boxes})

        if len(pendentes) >= LOTE:
            enviar()
        if (i + 1) % LOTE == 0:
            if cli.job_heartbeat(job_id, {"feito": i + 1, "de": len(alvos)}):
                cancelou = True
                log("cancelamento pedido — encerrando", "warn")
                break
            log(f"{i + 1}/{len(alvos)} imagens, {total_boxes} caixas até aqui")

    enviar()
    if cancelou:
        cli.finish(job_id, "canceled", error="cancelado pelo usuário")
        return
    log(f"pronto: {total_boxes} caixas em {len(alvos)} imagens")
    cli.finish(job_id, "succeeded",
               result={"pretags": total_boxes, "imagens": len(alvos)})
