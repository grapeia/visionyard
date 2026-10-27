"""Parâmetros de treino compartilhados pela API e pelo agente (só stdlib).

A lista é fechada: caminhos, retomada e mudanças no vocabulário não podem
sobrepor o dataset congelado nem o destino dos pesos que o agente publica.
Ausência significa usar o padrão da Ultralytics instalada, não um default
copiado que envelhece separado dela.
"""
from __future__ import annotations

import ast
import json
import math
from typing import Any

BASICOS = {"epochs", "imgsz", "batch"}
CAMPOS: dict[str, dict[str, Any]] = {}


def _campo(nome: str, tipo: str, grupo: str, descricao: str,
           minimo=None, maximo=None, choices=None, nullable=False, tasks=None) -> None:
    CAMPOS[nome] = {"name": nome, "type": tipo, "group": grupo,
                    "description": descricao, "min": minimo, "max": maximo,
                    "choices": choices, "nullable": nullable,
                    "tasks": tasks or ["detect", "segment"]}


for nome, minimo, maximo, descricao in [
    ("epochs", 1, 1000, "Número máximo de épocas"),
    ("imgsz", 64, 2048, "Tamanho de entrada em pixels"),
    ("batch", 1, 256, "Imagens por lote"),
    ("patience", 0, 1000, "Épocas sem melhora antes de parar; 0 desativa"),
    ("workers", 0, 64, "Processos de leitura de imagens"),
    ("seed", 0, 2147483647, "Semente aleatória"),
    ("close_mosaic", 0, 1000, "Épocas finais sem mosaic"),
    ("nbs", 1, 1024, "Batch nominal para acumulação de gradientes"),
    ("max_det", 1, 10000, "Detecções máximas por imagem na validação"),
    ("save_period", -1, 1000, "Intervalo entre checkpoints; -1 desativa"),
]:
    _campo(nome, "int", "Execução", descricao, minimo, maximo)

for nome, limite_min, limite_max, grupo, descricao in [
    ("time", 0.001, 10000, "Execução", "Limite de horas; substitui o limite de épocas"),
    ("fraction", 0.001, 1, "Execução", "Fração das imagens de treino utilizada"),
    ("lr0", 0, 1, "Otimizador", "Taxa de aprendizado inicial"),
    ("lrf", 0, 1, "Otimizador", "Fator da taxa final em relação à inicial"),
    ("momentum", 0, 0.999999, "Otimizador", "Momentum ou beta1 do Adam"),
    ("weight_decay", 0, 1, "Otimizador", "Regularização dos pesos"),
    ("warmup_epochs", 0, 1000, "Otimizador", "Épocas de aquecimento"),
    ("warmup_momentum", 0, 0.999999, "Otimizador", "Momentum inicial do aquecimento"),
    ("warmup_bias_lr", 0, 1, "Otimizador", "Taxa inicial do bias no aquecimento"),
    ("box", 0, 100, "Perdas", "Peso da perda de localização"),
    ("cls", 0, 100, "Perdas", "Peso da perda de classe"),
    ("dfl", 0, 100, "Perdas", "Peso da perda DFL"),
    ("conf", 0, 1, "Validação", "Confiança mínima na validação"),
    ("iou", 0, 1, "Validação", "IoU do NMS na validação"),
    ("hsv_h", 0, 1, "Transformações", "Variação de matiz"),
    ("hsv_s", 0, 1, "Transformações", "Variação de saturação"),
    ("hsv_v", 0, 1, "Transformações", "Variação de brilho"),
    ("degrees", 0, 180, "Transformações", "Rotação máxima em graus"),
    ("translate", 0, 1, "Transformações", "Deslocamento relativo"),
    ("scale", 0, 1, "Transformações", "Variação de escala"),
    ("shear", 0, 180, "Transformações", "Cisalhamento em graus"),
    ("perspective", 0, 0.001, "Transformações", "Perspectiva"),
    ("flipud", 0, 1, "Transformações", "Probabilidade de espelhar verticalmente"),
    ("fliplr", 0, 1, "Transformações", "Probabilidade de espelhar horizontalmente"),
    ("bgr", 0, 1, "Transformações", "Probabilidade de trocar canais RGB/BGR"),
    ("mosaic", 0, 1, "Transformações", "Probabilidade de mosaic"),
    ("mixup", 0, 1, "Transformações", "Probabilidade de mixup"),
    ("copy_paste", 0, 1, "Segmentação", "Probabilidade de copiar máscaras"),
]:
    _campo(nome, "float", grupo, descricao, limite_min, limite_max,
           nullable=nome in {"time", "conf"},
           tasks=["segment"] if nome == "copy_paste" else None)

for nome, grupo, descricao in [
    ("deterministic", "Execução", "Usar algoritmos determinísticos"),
    ("rect", "Execução", "Lotes com imagens de proporções semelhantes"),
    ("cos_lr", "Otimizador", "Agenda de taxa de aprendizado por cosseno"),
    ("amp", "Execução", "Precisão mista automática"),
    ("multi_scale", "Transformações", "Variar resolução entre lotes"),
    ("plots", "Validação", "Gerar gráficos durante o treino"),
    ("verbose", "Execução", "Saída detalhada da biblioteca"),
    ("overlap_mask", "Segmentação", "Permitir sobreposição de máscaras"),
]:
    _campo(nome, "bool", grupo, descricao,
           tasks=["segment"] if nome == "overlap_mask" else None)
_campo("mask_ratio", "int", "Segmentação", "Fator de redução das máscaras", 1, 64,
       tasks=["segment"])
_campo("optimizer", "choice", "Otimizador", "Auto pode substituir lr0 e momentum",
       choices=["auto", "SGD", "Adam", "AdamW", "Adamax", "NAdam", "RAdam", "RMSProp"])
_campo("copy_paste_mode", "choice", "Segmentação", "Modo de cópia de máscaras",
       choices=["flip", "mixup"], tasks=["segment"])
_campo("cache", "cache", "Execução", "Cache das imagens: false, true, ram ou disk")
_campo("freeze", "freeze", "Execução", "Congelar N camadas ou lista de índices",
       nullable=True)
_campo("device", "device", "Execução", "GPU local (0) ou cpu; não é a máquina do pool",
       nullable=True)

GERIDOS = {"data", "model", "project", "name", "exist_ok", "save", "val", "split",
           "resume", "cfg", "single_cls", "pretrained", "task", "mode"}
FORA_DO_TREINO = {
    "profile", "dropout", "pose", "kobj", "auto_augment", "erasing", "crop_fraction",
    "save_json", "save_hybrid", "half", "dnn", "source", "vid_stride", "stream_buffer",
    "visualize", "augment", "agnostic_nms", "classes", "retina_masks", "embed", "show",
    "save_frames", "save_txt", "save_conf", "save_crop", "show_labels", "show_conf",
    "show_boxes", "line_width", "format", "keras", "optimize", "int8", "dynamic",
    "simplify", "opset", "workspace", "nms", "tracker",
}

PRESET_DEV = {
    "epochs": 300, "imgsz": 416, "batch": 80, "time": None, "patience": 150,
    "save_period": -1, "cache": False, "device": 0, "workers": 8, "optimizer": "Adam",
    "verbose": True, "seed": 0, "deterministic": True, "rect": False, "cos_lr": False,
    "close_mosaic": 10, "amp": True, "fraction": 1.0, "freeze": None,
    "multi_scale": False, "overlap_mask": True, "mask_ratio": 4, "conf": None,
    "iou": 0.7, "max_det": 300, "plots": True,
    "lr0": 0.01, "lrf": 0.01, "momentum": 0.937, "weight_decay": 0.0005,
    "warmup_epochs": 3.0, "warmup_momentum": 0.8, "warmup_bias_lr": 0.1,
    "box": 7.5, "cls": 0.5, "dfl": 1.5, "nbs": 64,
    "hsv_h": 0.0, "hsv_s": 0.0, "hsv_v": 0.0, "degrees": 0.0,
    "translate": 0.0, "scale": 0.0, "shear": 0.0, "perspective": 0.0,
    "flipud": 0.0, "fliplr": 0.5, "bgr": 0.0, "mosaic": 0.0,
    "mixup": 0.0, "copy_paste": 0.0, "copy_paste_mode": "flip",
}


def motivo_exclusao(nome: str, tarefa: str) -> str | None:
    if nome in GERIDOS:
        return "gerenciado pelo sistema (dataset, modelo, saída ou ciclo de treino)"
    if nome in FORA_DO_TREINO:
        return "não configurável no treino de detecção/segmentação"
    if nome not in CAMPOS:
        return "parâmetro desconhecido"
    if tarefa not in CAMPOS[nome]["tasks"]:
        return "disponível apenas em segmentação"
    return None


def normalizar(params: dict, tarefa: str | None = None) -> dict:
    """Valida sem coerções silenciosas de bool para int, nem caminhos livres."""
    if not isinstance(params, dict) or len(params) > 100:
        raise ValueError("parâmetros devem ser um objeto de até 100 campos")
    saida: dict[str, Any] = {}
    for nome, valor in params.items():
        if not isinstance(nome, str) or nome not in CAMPOS:
            raise ValueError(f"parâmetro não permitido: {nome}")
        campo = CAMPOS[nome]
        if tarefa and tarefa not in campo["tasks"]:
            raise ValueError(f"{nome}: disponível apenas em segmentação")
        if valor is None and campo["nullable"]:
            saida[nome] = None
            continue
        tipo = campo["type"]
        valido = False
        if tipo in {"int", "float"}:
            valido = (type(valor) in ({int} if tipo == "int" else {int, float})
                      and campo["min"] <= valor <= campo["max"]
                      and math.isfinite(valor))
        elif tipo == "bool":
            valido = type(valor) is bool
        elif tipo == "choice" and isinstance(valor, str):
            escolhas = {x.lower(): x for x in campo["choices"]}
            valido = valor.lower() in escolhas
            valor = escolhas.get(valor.lower(), valor)
        elif tipo == "cache":
            valido = type(valor) is bool or valor in ("ram", "disk")
        elif tipo == "freeze":
            valido = (type(valor) is int and 0 <= valor <= 1000) or (
                isinstance(valor, list) and len(valor) <= 128
                and all(type(v) is int and 0 <= v <= 1000 for v in valor))
        elif tipo == "device":
            valido = (type(valor) is int and 0 <= valor <= 63) or valor == "cpu"
            if isinstance(valor, str) and valor.isdecimal() and len(valor) <= 2:
                valor = int(valor)
                valido = 0 <= valor <= 63
        if not valido:
            raise ValueError(f"{nome}: valor inválido; confira tipo e limites do campo")
        saida[nome] = valor
    return saida


def importar_texto(texto: str, tarefa: str) -> dict:
    if len(texto) > 50000:
        raise ValueError("configuração excede 50 mil caracteres")
    texto = texto.strip()
    try:
        bruto = json.loads(texto)
    except ValueError:
        try:
            bruto = ast.literal_eval(texto)
        except (ValueError, SyntaxError, RecursionError) as exc:
            raise ValueError(
                "informe um objeto JSON ou dicionário Python literal") from exc
    if not isinstance(bruto, dict):
        raise ValueError("configuração deve ser um objeto ou dicionário")
    aceitos, ignorados = {}, []
    for nome, valor in bruto.items():
        if not isinstance(nome, str):
            raise ValueError("nomes de parâmetros devem ser texto")
        motivo = motivo_exclusao(nome, tarefa)
        if motivo:
            ignorados.append({"name": nome, "reason": motivo})
        else:
            aceitos[nome] = valor
    return {"params": normalizar(aceitos, tarefa), "ignored": ignorados}


def catalogo(tarefa: str) -> dict:
    return {"fields": [v for v in CAMPOS.values() if tarefa in v["tasks"]],
            "preset_dev": {k: v for k, v in PRESET_DEV.items()
                           if tarefa in CAMPOS[k]["tasks"]},
            "defaults": {"epochs": 100, "imgsz": 640, "batch": 16}}
