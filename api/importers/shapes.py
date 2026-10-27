"""Geometria de origem validada antes de chegar ao banco."""
import math

from .base import ImportReport, SourceBox, SourcePolygon, SourceSample


def _unit(v: float, tolerancia: float) -> float:
    if not math.isfinite(v) or v < -tolerancia or v > 1 + tolerancia:
        raise ValueError("coordenada inválida ou fora da imagem")
    return max(0.0, min(1.0, v))


def polygon(nome: str, pontos: list,
            tolerancia: tuple[float, float] = (1e-9, 1e-9)) -> SourcePolygon:
    pts = [(_unit(float(x), tolerancia[0]), _unit(float(y), tolerancia[1]))
           for x, y in pontos]
    if len(pts) < 3:
        raise ValueError("polígono inválido")
    area = abs(sum(x * pts[(i + 1) % len(pts)][1]
                   - y * pts[(i + 1) % len(pts)][0]
                   for i, (x, y) in enumerate(pts))) / 2
    if len(set(pts)) < 3 or area < 1e-9:
        raise ValueError("polígono sem área")
    return SourcePolygon(nome, pts)


def rectangle(nome: str, x: float, y: float, w: float, h: float,
              tolerancia: tuple[float, float] = (1e-9, 1e-9)) -> SourceBox:
    if not all(math.isfinite(v) for v in (x, y, w, h)) or w <= 0 or h <= 0:
        raise ValueError("retângulo inválido")
    x0, y0 = _unit(x, tolerancia[0]), _unit(y, tolerancia[1])
    x1, y1 = _unit(x + w, tolerancia[0]), _unit(y + h, tolerancia[1])
    if x1 <= x0 or y1 <= y0:
        raise ValueError("retângulo sem área")
    return SourceBox(nome, (x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0)


def ls_results(items: object, sample: SourceSample, report: ImportReport) -> None:
    if not isinstance(items, list):
        report.skip(sample.key, "result deve ser uma lista")
        sample.annotation_incomplete = True
        return
    for item in items:
        try:
            tipo = item["type"]
            if tipo in {"choices", "textarea"}:
                report.warn(f"{sample.key}: metadado '{tipo}' não importado")
                continue
            if tipo not in {"rectanglelabels", "polygonlabels"}:
                raise ValueError(f"tipo '{tipo}' não suportado")
            value = item["value"]
            nomes = value[tipo]
            if not isinstance(nomes, list) or not nomes:
                raise ValueError("forma sem classe")
            if float(value.get("rotation") or 0):
                raise ValueError("bbox com rotação — não suportado")
            if float(item.get("image_rotation") or 0):
                raise ValueError("imagem com rotação — não suportado")
            sample.width = sample.width or item.get("original_width")
            sample.height = sample.height or item.get("original_height")
            for nome in nomes:
                if not isinstance(nome, str) or not nome:
                    raise ValueError("classe inválida")
                if tipo == "polygonlabels":
                    sample.polygons.append(polygon(nome, [
                        (float(x) / 100, float(y) / 100) for x, y in value["points"]]))
                else:
                    x, y, w, h = (float(value[k]) / 100
                                  for k in ("x", "y", "width", "height"))
                    sample.boxes.append(rectangle(nome, x, y, w, h))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            report.skip(sample.key, f"forma do LS inválida: {exc}")
            sample.annotation_incomplete = True
