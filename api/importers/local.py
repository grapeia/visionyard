"""Caminhos locais de origens: o conteúdo do JSON não pode escapar da raiz."""
from pathlib import Path

from .base import ImportReport


def image_path(root: Path, relative: str, report: ImportReport | None = None) -> Path:
    raiz = root.resolve()
    relativo = relative.replace("\\", "/")
    candidato = (raiz / relativo).resolve()
    if not candidato.is_relative_to(raiz):
        raise ValueError("caminho da imagem fora de image_root")
    if not candidato.is_file():
        candidato = (raiz / Path(relativo).name).resolve()
        if report is not None and candidato.is_file():
            report.warn(f"imagem '{relative}' resolvida apenas pelo nome do arquivo; "
                        "confira se corresponde à imagem anotada")
    if not candidato.is_relative_to(raiz):
        raise ValueError("caminho da imagem fora de image_root")
    if not candidato.is_file():
        raise ValueError("arquivo não encontrado em image_root")
    return candidato
