"""Executor do job `prelabel`: roda um modelo sobre as imagens do escopo e
devolve as caixas previstas — ou os contornos, quando o modelo é -seg.

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
# Tolerância da simplificação do contorno, em fração da imagem: 0,2% é
# ~2 px numa foto de 1000 px, abaixo do que a pessoa corrigiria à mão.
EPS_CONTORNO = 0.002


def _linha(pts: list, eps: float) -> list:
    """Douglas–Peucker iterativo sobre uma linha aberta."""
    if len(pts) < 3:
        return list(pts)
    manter = [False] * len(pts)
    manter[0] = manter[-1] = True
    pilha = [(0, len(pts) - 1)]
    while pilha:
        a, b = pilha.pop()
        (ax, ay), (bx, by) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        n = (dx * dx + dy * dy) ** 0.5 or 1.0
        dmax, idx = 0.0, -1
        for i in range(a + 1, b):
            d = abs(dy * (pts[i][0] - ax) - dx * (pts[i][1] - ay)) / n
            if d > dmax:
                dmax, idx = d, i
        if dmax > eps:
            manter[idx] = True
            pilha += [(a, idx), (idx, b)]
    return [p for p, m in zip(pts, manter) if m]  # noqa: B905 (3.8 não tem strict)


def simplificar(contorno: list, eps: float = EPS_CONTORNO) -> list:
    """Contorno fechado (último ponto liga no primeiro) → menos vértices.

    Parte o anel no ponto mais longe do início: Douglas–Peucker contra um
    segmento de comprimento zero (início = fim) descartaria tudo."""
    pts = [(float(x), float(y)) for x, y in contorno]
    if len(pts) < 4:
        return pts
    x0, y0 = pts[0]
    longe = max(range(len(pts)),
                key=lambda i: (pts[i][0] - x0) ** 2 + (pts[i][1] - y0) ** 2)
    ida = _linha(pts[:longe + 1], eps)
    volta = _linha([*pts[longe:], pts[0]], eps)
    return ida + volta[1:-1]


def _area(pts: list) -> float:
    return abs(sum(pts[i - 1][0] * p[1] - p[0] * pts[i - 1][1]
                   for i, p in enumerate(pts))) / 2


def previsoes(res) -> list:
    """Resultado de `model.predict` → itens que a API grava.

    Modelo -seg traz `res.masks`: o item é o contorno (`pts`), que é o que o
    projeto de segmentação anota. Sem máscara, é a caixa. A API confere que
    a forma bate com o tipo do projeto e recusa o lote se não bater."""
    masks = getattr(res, "masks", None)
    out = []
    if masks is not None:
        for b, contorno in zip(res.boxes, masks.xyn):  # noqa: B905 (3.8 não tem strict)
            pts = simplificar(contorno)
            if len(set(pts)) < 3 or _area(pts) <= 0:
                continue          # máscara de um pixel ou fio: não é objeto
            out.append({"cls": int(b.cls[0]),
                        "pts": [[round(x, 6), round(y, 6)] for x, y in pts],
                        "conf": round(float(b.conf[0]), 4)})
        return out
    for b in res.boxes:
        cx, cy, w, h = b.xywhn[0].tolist()   # normalizado 0..1, centro
        out.append({"cls": int(b.cls[0]), "cx": cx, "cy": cy,
                    "w": w, "h": h, "conf": round(float(b.conf[0]), 4)})
    return out


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
        boxes = previsoes(res)
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
