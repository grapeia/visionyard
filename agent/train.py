"""Executor do job `train`: YOLO (Ultralytics) com telemetria ao vivo.

O treino em si roda num SUBPROCESSO fresco (`agent.train_worker`), não no daemon:
rodá-lo no processo de vida longa deixava a validação com `val_loss=nan` em
alguns datasets (estado de processo/GPU acumulado), zerando o mAP. Aqui a gente
materializa o dataset, sobe o subprocesso, encaminha as métricas que ele emite
por época (para a tela acompanhar ao vivo), cancela via SIGTERM se pedido, e ao
fim publica o peso. O acoplamento com o Ultralytics vive todo no worker.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from contracts import jobs

from . import gcs
from .client import Client
from .materialize import materializar, roi_tupla
from .train_worker import MARCADOR


def executar(cli: Client, job: dict, work_dir: Path, models_bucket: str,
             models_prefix: str) -> None:
    job_id = job["id"]
    p = jobs.Train.de_payload(job["payload"])
    run_id = p.run_id
    params = p.params

    seq = {"n": 0}

    def log(msg: str, level: str = "info") -> None:
        seq["n"] += 1
        cli.events(job_id, [{"seq": seq["n"], "level": level, "message": msg}])

    # ------------------------------------------------------------------ #
    # 1. Materializar a versão do dataset a partir do manifesto
    # ------------------------------------------------------------------ #
    meta = cli.manifest_meta(job_id)
    classes = meta["classes"]
    roi = roi_tupla(meta.get("roi"))
    log(f"versão {p.version} de {p.dataset_slug}: "
        f"{meta['stats']['imagens']} imagens, {len(classes)} classes"
        + (f" — recorte ROI {roi}" if roi else ""))

    raiz = work_dir / run_id
    items = raiz / "items.jsonl"
    items.parent.mkdir(parents=True, exist_ok=True)
    cli.download_items(job_id, str(items))
    conta = materializar(items, classes, raiz / "ds",
                         work_dir / "cache-imagens", log, roi=roi)

    # ------------------------------------------------------------------ #
    # 2. Treinar num subprocesso fresco, transmitindo métricas por época
    # ------------------------------------------------------------------ #
    epochs = params.get("epochs", 100)
    cfg = {"data": conta["data_yaml"], "epochs": epochs,
           "imgsz": params.get("imgsz", 640),
           "batch": params.get("batch", 16),
           "base_model": p.base_model,
           "project": str(raiz / "runs")}
    raiz_repo = Path(__file__).resolve().parent.parent

    cancelado = {"v": False}
    melhor_map = -1.0
    melhor_metrics: dict = {}
    recentes: list[str] = []   # últimas linhas não-métrica, p/ diagnóstico

    log("iniciando o treino")
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent.train_worker", json.dumps(cfg)],
        cwd=str(raiz_repo), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1)

    saida = proc.stdout
    if saida is None:      # não acontece com stdout=PIPE; o Optional é da assinatura
        raise RuntimeError("subprocesso de treino subiu sem saída para ler")

    # o worker emite uma linha `@@EPOCH@@ {json}` por época; o resto (ruído do
    # Ultralytics) é ignorado, guardando só as últimas linhas p/ diagnóstico.
    for linha in saida:                           # bloqueia até a próxima linha
        linha = linha.rstrip("\n")
        if not linha.startswith(MARCADOR):
            if linha.strip():
                recentes.append(linha)
                del recentes[:-15]
            continue
        ev = json.loads(linha[len(MARCADOR):])
        epoch, norm = ev["epoch"], ev["metrics"]
        if norm:
            cli.metrics(job_id, [{"epoch": epoch, "key": k, "value": v}
                                 for k, v in norm.items()])
            if norm.get("mAP50-95", -1) > melhor_map:
                melhor_map = norm["mAP50-95"]
                melhor_metrics = norm
        log(f"época {epoch}/{epochs}: mAP50-95={norm.get('mAP50-95', 0):.4f}")
        if cli.job_heartbeat(job_id, {"epoch": epoch, "de": epochs}):
            cancelado["v"] = True
            log("cancelamento pedido — encerrando o treino", "warn")
            proc.terminate()                      # SIGTERM → parada limpa
    proc.wait()

    if cancelado["v"]:
        cli.finish(job_id, "canceled", error="cancelado pelo usuário")
        return
    if proc.returncode != 0:
        cli.finish(job_id, "failed",
                   error="treino falhou (código {}): {}".format(
                       proc.returncode, " | ".join(recentes[-5:]) or "sem saída"))
        return

    # ------------------------------------------------------------------ #
    # 3. Publicar o peso e registrar a versão do modelo
    # ------------------------------------------------------------------ #
    best = raiz / "runs" / "train" / "weights" / "best.pt"
    if not best.is_file():
        cli.finish(job_id, "failed", error=f"peso não encontrado: {best}")
        return

    if not models_bucket:
        cli.finish(job_id, "failed",
                   error="VISIONYARD_MODELS_BUCKET não configurado — sem onde "
                         "publicar o peso")
        return

    destino = (f"gs://{models_bucket}/{models_prefix}/"
               f"{p.project_slug}/{p.model_slug}/{run_id}/best.pt")
    log(f"publicando peso em {destino}")
    gcs.enviar(best, destino)
    sha = gcs.sha256_arquivo(best)
    reg = cli.register_model(job_id, destino, sha, best.stat().st_size,
                             melhor_metrics)
    log(f"modelo {reg['model_slug']} v{reg['version']} registrado")
    cli.finish(job_id, "succeeded",
               result={"model_version_id": reg["model_version_id"],
                       "version": reg["version"], "metrics": melhor_metrics,
                       "materializacao": {"train": conta["train"],
                                          "val": conta["val"]}})
