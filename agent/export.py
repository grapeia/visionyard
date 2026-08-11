"""Executor do job `export`: compila uma versão do modelo para ONNX ou engine
TensorRT e registra o build.

O engine TensorRT é específico do dispositivo (JetPack/TRT/precisão), por isso é
compilado NA máquina-alvo. O build guarda em qual device e com que alvo saiu, e
uma medida de latência ali mesmo — é o número que decide se cabe no tempo da
linha.
"""
from __future__ import annotations

import os
import platform
from pathlib import Path

from . import gcs
from .client import Client


def _detectar_alvo(payload: dict) -> dict:
    alvo = {"precision": payload.get("precision", "fp16"),
            "imgsz": payload.get("imgsz", 640), "dla": bool(payload.get("dla")),
            "dynamic": bool(payload.get("dynamic"))}
    # JetPack/TRT, quando dá para descobrir — entra no alvo para rastrear
    for chave, env in (("jetpack", "JETPACK_VERSION"), ("trt", "TRT_VERSION")):
        v = os.environ.get(env)
        if v:
            alvo[chave] = v
    alvo["arch"] = platform.machine()
    return alvo


def executar(cli: Client, job: dict, work_dir: Path, models_bucket: str,
             models_prefix: str) -> None:
    from ultralytics import YOLO

    job_id = job["id"]
    payload = job["payload"]
    fmt = payload.get("format", "engine")
    imgsz = payload.get("imgsz", 640)
    precision = payload.get("precision", "fp16")
    seq = {"n": 0}

    def log(msg: str, level: str = "info") -> None:
        seq["n"] += 1
        cli.events(job_id, [{"seq": seq["n"], "level": level, "message": msg}])

    if not models_bucket:
        cli.finish(job_id, "failed",
                   error="VISIONYARD_MODELS_BUCKET não configurado — sem onde publicar o build")
        return

    raiz = work_dir / job_id
    raiz.mkdir(parents=True, exist_ok=True)
    peso = raiz / "model.pt"
    gcs.baixar(payload["weights_uri"], peso)
    log(f"compilando v{payload.get('version')} para {fmt} "
        f"({precision}, imgsz={imgsz}"
        + (", dinâmico" if payload.get("dynamic") else "") + ")")
    cli.job_heartbeat(job_id, {"etapa": "export"})

    modelo = YOLO(str(peso))
    kwargs = {"format": fmt, "imgsz": imgsz}
    if fmt == "engine":
        kwargs["half"] = precision == "fp16"
        kwargs["int8"] = precision == "int8"
        # dynamic iguala o letterbox ao do .pt (DLA não aceita — a API já zera)
        kwargs["dynamic"] = bool(payload.get("dynamic"))
        if payload.get("dla"):
            kwargs["device"] = "dla:0"
    saida = Path(modelo.export(**kwargs))
    log(f"gerado {saida.name}")

    import numpy as np
    dummy = np.zeros((imgsz, imgsz, 3), dtype="uint8")
    # Carregar e rodar o artefato compilado é o smoke-test de que ele funciona.
    # Se falhar, o build NÃO pode ser publicado como bom — um engine corrompido
    # passaria e só quebraria no coletor depois do deploy.
    try:
        compilado = YOLO(str(saida))
        compilado.predict(dummy, imgsz=imgsz, verbose=False)   # aquecimento + smoke
    except Exception as e:  # noqa: BLE001
        cli.finish(job_id, "failed",
                   error=f"o artefato compilado não carrega/roda: {e}")
        return

    # Só a MEDIÇÃO de latência é best-effort (o número que decide se cabe no
    # tempo da linha); um erro de timing não invalida um engine que já rodou.
    bench = {}
    try:
        import time
        amostras = []
        for _ in range(8):
            ini = time.monotonic()
            compilado.predict(dummy, imgsz=imgsz, verbose=False)
            amostras.append((time.monotonic() - ini) * 1000)
        amostras.sort()
        bench = {"p50_ms": round(amostras[len(amostras) // 2], 2),
                 "p95_ms": round(amostras[-1], 2)}
        log(f"latência p50={bench['p50_ms']}ms p95={bench['p95_ms']}ms")
    except Exception as e:  # noqa: BLE001
        log(f"latência não medida: {e}", "warn")

    destino = (f"gs://{models_bucket}/{models_prefix}/"
               f"{payload.get('project_slug')}/{payload.get('model_slug')}/"
               f"v{payload.get('version')}/builds/{saida.name}")
    gcs.enviar(saida, destino)
    sha = gcs.sha256_arquivo(saida)
    reg = cli.register_build(job_id, fmt, destino, sha, saida.stat().st_size,
                             _detectar_alvo(payload), bench)
    log(f"build registrado ({fmt})" + (" — já existia" if reg.get("ja_existia") else ""))
    cli.finish(job_id, "succeeded",
               result={"format": fmt, "uri": destino, "bench": bench})
