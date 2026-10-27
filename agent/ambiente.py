"""Versão do agente e o ambiente em que ele mede.

A avaliação grava onde foi medida — GPU, driver, CUDA, runtime — porque tempo
de inferência só se compara entre medições feitas no mesmo hardware e com o
mesmo software. Tudo aqui é melhor esforço: o que não dá para descobrir fica
de fora, e nada daqui pode derrubar um job.
"""
from __future__ import annotations

import platform
import re
import subprocess
from functools import lru_cache
from pathlib import Path

AGENT_VERSION = "0.3.0"

# formato do artefato → quem o executa na avaliação; são os três formatos que
# `model_build.format` aceita
RUNTIME = {".pt": "pytorch", ".onnx": "onnxruntime", ".engine": "tensorrt"}


def _rodar(*cmd: str) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5,
                           check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None


@lru_cache(maxsize=None)  # noqa: UP033 — functools.cache é 3.9+; o Advantech roda 3.8
def versao() -> str:
    """`0.3.0+<sha>` quando o agente roda de um checkout git, senão só a
    versão: duas máquinas com o mesmo número e código diferente se distinguem."""
    sha = _rodar("git", "-C", str(Path(__file__).resolve().parent),
                 "rev-parse", "--short", "HEAD")
    return f"{AGENT_VERSION}+{sha}" if sha else AGENT_VERSION


def _versao_de(modulo: str) -> str | None:
    try:
        return __import__(modulo).__version__
    except Exception:
        return None


def _driver() -> str | None:
    """Driver NVIDIA pelo nvidia-smi; no Jetson, que não tem nvidia-smi, a
    release do L4T faz esse papel."""
    v = _rodar("nvidia-smi", "--query-gpu=driver_version",
               "--format=csv,noheader")
    if v:
        return v.splitlines()[0].strip()
    try:
        return versao_l4t(Path("/etc/nv_tegra_release").read_text())
    except OSError:
        return None


def versao_l4t(texto: str) -> str | None:
    """`# R35 (release), REVISION: 3.1, ...` → `L4T R35.3.1`. A release sozinha
    (R35) junta versões de JetPack com TensorRT diferente."""
    m = re.search(r"R(\d+)\b.*?REVISION:\s*([\d.]+)", texto)
    return f"L4T R{m.group(1)}.{m.group(2)}" if m else None


def ambiente(artefato: Path) -> dict:
    """O que se sabe da máquina e do software no momento da medição."""
    amb: dict = {"host": platform.node(), "os": platform.platform(),
                 "arch": platform.machine(),
                 "python": platform.python_version(),
                 "runtime": RUNTIME.get(artefato.suffix, artefato.suffix),
                 "agent_version": versao()}
    try:
        import torch
        amb["torch"] = torch.__version__
        if torch.cuda.is_available():
            amb["gpu"] = torch.cuda.get_device_name(0)
            amb["cuda"] = torch.version.cuda
    except Exception:
        pass
    # o runtime que executa o artefato importa mais que os outros instalados
    for chave, modulo in (("ultralytics", "ultralytics"), ("trt", "tensorrt"),
                          ("onnxruntime", "onnxruntime"),
                          ("openvino", "openvino")):
        v = _versao_de(modulo)
        if v:
            amb[chave] = v
    driver = _driver()
    if driver:
        amb["driver"] = driver
    return {k: v for k, v in amb.items() if v}
