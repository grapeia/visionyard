"""Subprocesso de treino: roda a Ultralytics isolada do daemon do agente.

O daemon é um processo de vida longa. Rodar o treino nele deixava a validação
com `val_loss=nan` em alguns datasets (estado de processo/GPU acumulado ao longo
do dia) — o modelo nunca aprendia a disparar e o mAP ficava em 0. Um processo
FRESCO por job elimina isso: o mesmo dado que dava 0 no daemon treina a 0.9+ num
processo novo. (Diagnóstico de 2026-07-21: reproduzível só no daemon vivo.)

Protocolo com o pai (`agent/train.py`):
  * uma linha ``@@EPOCH@@ {"epoch":N,"metrics":{...}}`` por época, em stdout;
  * SIGTERM = cancelar → parada limpa do Ultralytics no fim/início da época.
O peso final sai no caminho padrão: ``<project>/train/weights/best.pt``.

Rodado como ``python -m agent.train_worker '<config-json>'`` a partir da raiz do
repo. Sem imports do resto do agente (nem rede): é só o treino, isolado.
"""
from __future__ import annotations

import json
import math
import signal
import sys

# Colunas do Ultralytics → chaves normalizadas que a API guarda. Nomes estáveis
# aqui blindam a tela de mudanças de nome entre versões do framework.
KEYMAP = {
    "metrics/precision(B)": "precision",
    "metrics/recall(B)": "recall",
    "metrics/mAP50(B)": "mAP50",
    "metrics/mAP50-95(B)": "mAP50-95",
    "val/box_loss": "val_box_loss",
    "val/cls_loss": "val_cls_loss",
}

MARCADOR = "@@EPOCH@@ "
_parar = {"v": False}


def _emitir(obj: dict) -> None:
    sys.stdout.write(MARCADOR + json.dumps(obj) + "\n")
    sys.stdout.flush()


def normalizar(trainer) -> dict:
    """Métricas do trainer → dict {chave: valor finito}."""
    bruto = dict(getattr(trainer, "metrics", {}) or {})
    try:
        bruto.update(trainer.label_loss_items(trainer.tloss, prefix="train"))
    except Exception:      # perda de treino é bônus, não essencial
        pass
    norm: dict = {}
    for col, val in bruto.items():
        chave = KEYMAP.get(col)
        if chave is None and col.startswith("train/"):
            chave = col.split("/", 1)[1]     # box_loss, cls_loss, dfl_loss
        if chave is None:
            continue
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        # NaN/inf são normais nas primeiras épocas (mAP sem detecção); o JSON
        # não os aceita e o POST do pai estouraria. Não são métrica: pula.
        if math.isfinite(v):
            norm[chave] = v
    return norm


def main() -> int:
    cfg = json.loads(sys.argv[1])
    from ultralytics import YOLO

    # SIGTERM (pai pedindo cancelamento) marca a flag; o Ultralytics para no
    # próximo limite de época, sem derrubar o processo à força.
    signal.signal(signal.SIGTERM, lambda *_: _parar.update(v=True))

    def parar_se_pedido(trainer):
        if _parar["v"]:
            trainer.stop = True

    def ao_fim_da_epoca(trainer):
        epoch = int(getattr(trainer, "epoch", 0)) + 1
        _emitir({"epoch": epoch, "metrics": normalizar(trainer)})
        parar_se_pedido(trainer)

    modelo = YOLO(cfg.get("base_model", "yolo11n.pt"))
    modelo.add_callback("on_train_epoch_start", parar_se_pedido)
    modelo.add_callback("on_fit_epoch_end", ao_fim_da_epoca)
    modelo.train(data=cfg["data"], epochs=cfg.get("epochs", 100),
                 imgsz=cfg.get("imgsz", 640), batch=cfg.get("batch", 16),
                 project=cfg["project"], name="train", exist_ok=True,
                 verbose=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
