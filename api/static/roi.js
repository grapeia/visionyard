/* O recorte (ROI) do dataset.
 *
 * A ROI é uma caixa normalizada [x,y,w,h] em frações 0–1 que o pipeline recorta
 * no treino/avaliação/inferência. A UI trabalha em presets (âncora 3×3 +
 * largura%/altura%) e compila para a caixa; o backend guarda a caixa mais os
 * campos de preset para repopular o diálogo. */

import { abrirDataset } from "./datasets.js";
import { $, api, avisar, comTravaDeEnvio, estado } from "./nucleo.js";

const ANCHORS = ["topo-esq", "topo-centro", "topo-dir",
                 "meio-esq", "meio-centro", "meio-dir",
                 "base-esq", "base-centro", "base-dir"];
export const pctRoi = (v) => Math.round(v * 100);
const roiEdit = { anchor: "topo-centro", w: 40, h: 40, slug: null };

/** Preset (âncora + largura%/altura%) → caixa normalizada {x,y,w,h}. */
function compilarRoi(anchor, wPct, hPct) {
  const w = wPct / 100, h = hPct / 100;
  const [ver, hor] = anchor.split("-");
  const x = hor === "esq" ? 0 : hor === "dir" ? 1 - w : (1 - w) / 2;
  const y = ver === "topo" ? 0 : ver === "base" ? 1 - h : (1 - h) / 2;
  const r4 = (v) => Math.round(v * 1e4) / 1e4;
  return { x: r4(x), y: r4(y), w: r4(w), h: r4(h),
           anchor, w_pct: r4(w), h_pct: r4(h) };
}

/** Caixa salva sem preset (ex.: derivada de rótulos) → âncora mais próxima. */
function derivarAnchor(r) {
  const cx = r.x + r.w / 2, cy = r.y + r.h / 2;
  const faixa = (c) => c < 1 / 3 ? 0 : c > 2 / 3 ? 2 : 1;   // terços
  const hor = ["esq", "centro", "dir"][faixa(cx)];
  const ver = ["topo", "meio", "base"][faixa(cy)];
  return `${ver}-${hor}`;
}

export function abrirRoi(ds) {
  const r = ds.roi;
  if (r) {
    roiEdit.w = Math.round((r.w_pct != null ? r.w_pct : r.w) * 100);
    roiEdit.h = Math.round((r.h_pct != null ? r.h_pct : r.h) * 100);
    roiEdit.anchor = ANCHORS.includes(r.anchor) ? r.anchor : derivarAnchor(r);
  } else {
    roiEdit.anchor = "topo-centro"; roiEdit.w = 40; roiEdit.h = 40;
  }
  roiEdit.slug = ds.slug;
  sincronizarRoiUI();
  $("dlgRoi").showModal();
}

function sincronizarRoiUI() {
  $("roiGrade").querySelectorAll("[data-anchor]").forEach((b) =>
    b.setAttribute("aria-current", b.dataset.anchor === roiEdit.anchor));
  $("roiLarg").value = roiEdit.w;
  $("roiAlt").value = roiEdit.h;
  $("roiLargV").textContent = roiEdit.w + "%";
  $("roiAltV").textContent = roiEdit.h + "%";
  desenharRoiPreview();
}

/** Desenha a ROI (clara) sobre um quadro retrato, com o fora escurecido e
 *  hachurado — o mesmo vocabulário do overlay na rotulagem. */
function desenharRoiPreview() {
  const cv = $("roiPreview"), cx = cv.getContext("2d");
  const roi = compilarRoi(roiEdit.anchor, roiEdit.w, roiEdit.h);
  const x = roi.x * cv.width, y = roi.y * cv.height,
        w = roi.w * cv.width, h = roi.h * cv.height;
  cx.clearRect(0, 0, cv.width, cv.height);
  cx.fillStyle = "#0b0f14";
  cx.fillRect(0, 0, cv.width, cv.height);
  hachurarFora(cx, cv.width, cv.height, x, y, w, h);
  cx.strokeStyle = "#ffd60a";
  cx.lineWidth = 2;
  cx.strokeRect(x, y, w, h);
  cx.strokeStyle = "#2a3540";
  cx.lineWidth = 1;
  cx.strokeRect(0.5, 0.5, cv.width - 1, cv.height - 1);
  $("roiCoords").textContent =
    `x ${pctRoi(roi.x)}–${pctRoi(roi.x + roi.w)}% · ` +
    `y ${pctRoi(roi.y)}–${pctRoi(roi.y + roi.h)}%`;
}

/** Escurece e hachura a área FORA da caixa (rx,ry,rw,rh) num canvas. */
export function hachurarFora(cx, W, H, rx, ry, rw, rh) {
  cx.save();
  cx.beginPath();
  cx.rect(0, 0, W, H);
  cx.rect(rx, ry, rw, rh);
  cx.clip("evenodd");                     // recorta para só-o-fora
  cx.fillStyle = "rgba(5,8,11,.55)";
  cx.fillRect(0, 0, W, H);
  cx.strokeStyle = "rgba(255,214,10,.22)";
  cx.lineWidth = 1;
  for (let d = -H; d < W; d += 8) {
    cx.beginPath(); cx.moveTo(d, 0); cx.lineTo(d + H, H); cx.stroke();
  }
  cx.restore();
}

$("roiGrade").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-anchor]");
  if (!b) return;
  roiEdit.anchor = b.dataset.anchor;
  sincronizarRoiUI();
});
$("roiLarg").addEventListener("input", (ev) => {
  roiEdit.w = Number(ev.target.value); sincronizarRoiUI();
});
$("roiAlt").addEventListener("input", (ev) => {
  roiEdit.h = Number(ev.target.value); sincronizarRoiUI();
});

async function salvarRoi(roi) {
  await api(`/projects/${estado.projeto}/datasets/${roiEdit.slug}`,
            { method: "PATCH", body: JSON.stringify({ roi }) });
  $("dlgRoi").close();
  avisar(roi ? "Recorte salvo." : "Recorte removido.");
  await abrirDataset(roiEdit.slug);
}

$("roiRemover").addEventListener("click", () =>
  salvarRoi(null).catch((e) => avisar(e.message, true)));

$("formRoi").addEventListener("submit", comTravaDeEnvio($("formRoi"), async () => {
  try {
    await salvarRoi(compilarRoi(roiEdit.anchor, roiEdit.w, roiEdit.h));
  } catch (e) { avisar(e.message, true); }
}));
