"""LabelMe: um JSON por imagem, retângulos e polígonos em pixels."""
import json
from collections.abc import Iterator
from pathlib import Path

from PIL import Image

from .base import ImportReport, SourceSample
from .local import image_path
from .shapes import polygon, rectangle


class LabelMeImporter:
    kind = "labelme_json"

    def __init__(self, json_path: str, image_root: str, image_base_uri: str = ""):
        self.path = Path(json_path)
        self.image_root = Path(image_root)
        self.image_base_uri = image_base_uri.rstrip("/")
        if not self.path.exists():
            raise FileNotFoundError(json_path)

    def describe(self) -> str:
        return f"LabelMe (JSON) {self.path.name}"

    def classes(self) -> list[str]:
        nomes: set[str] = set()
        for sample in self.read(ImportReport()):
            nomes.update(f.cls_name for f in sample.boxes)
            nomes.update(f.cls_name for f in sample.polygons)
        return sorted(nomes)

    def read(self, report: ImportReport) -> Iterator[SourceSample]:
        arquivos = (sorted(self.path.glob("*.json"))
                    if self.path.is_dir() else [self.path])
        if not arquivos:
            report.skip(self.path.name, "nenhum JSON encontrado")
        for arquivo in arquivos:
            try:
                if not arquivo.resolve().is_relative_to(self.path.resolve().parent
                        if self.path.is_file() else self.path.resolve()):
                    raise ValueError("JSON fora da pasta de importação")
                data = json.loads(arquivo.read_text())
                rel = data["imagePath"]
                local = image_path(self.image_root, rel, report)
                with Image.open(local) as imagem:
                    w, h = imagem.size
                if (data.get("imageWidth", w) != w or data.get("imageHeight", h) != h):
                    raise ValueError("dimensões do JSON divergem da imagem")
                rel = local.relative_to(self.image_root.resolve()).as_posix()
                sample = SourceSample(key=arquivo.name, local_path=str(local),
                    uri=f"{self.image_base_uri}/{rel}" if self.image_base_uri else rel,
                    width=w, height=h, annotated=True)
                shapes = data["shapes"]
                if not isinstance(shapes, list):
                    raise ValueError("shapes deve ser uma lista")
                for shape in shapes:
                    try:
                        nome = shape["label"]
                        if not isinstance(nome, str) or not nome:
                            raise ValueError("forma sem classe")
                        pts = [(float(x) / w, float(y) / h) for x, y in shape["points"]]
                        tipo = shape.get("shape_type", "polygon")
                        if tipo == "polygon":
                            sample.polygons.append(polygon(
                                nome, pts, (0.5 / w, 0.5 / h)))
                        elif tipo == "rectangle" and len(pts) == 2:
                            (x1, y1), (x2, y2) = pts
                            sample.boxes.append(rectangle(
                                nome, min(x1, x2), min(y1, y2),
                                abs(x2 - x1), abs(y2 - y1), (0.5 / w, 0.5 / h)))
                        else:
                            raise ValueError(f"forma '{tipo}' não suportada")
                    except (KeyError, TypeError, ValueError, OverflowError) as exc:
                        report.skip(arquivo.name, f"forma LabelMe inválida: {exc}")
                        sample.annotation_incomplete = True
                yield sample
            except (OSError, UnicodeError, KeyError, TypeError, ValueError):
                report.skip(arquivo.name,
                            "JSON inválido ou imagem fora/ausente em image_root")
