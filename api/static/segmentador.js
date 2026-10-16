/* Segmentação no anotador: o contorno sai de um clique do SAM e se ajusta
 * vértice a vértice. Sem SAM (rede, CORS do bucket), o mesmo polígono se
 * desenha à mão, clicando nos vértices.
 *
 * Só importa de `nucleo.js`: quem conhece o estado da tela (`anot`) é o
 * `anotador.js`, que chama estas funções passando o que elas precisam. */

import { avisar, cor } from "./nucleo.js";

// Versão fixada: a API do processador do SAM mudou entre majors, e o
// protótipo (tmp/sam-prototipo) foi medido com esta.
const TRANSFORMERS = "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.8.1";
const MODELO_SAM = "Xenova/slimsam-77-uniform";
// O encoder do SAM trabalha em 1024 de qualquer jeito; reduzir antes deixa a
// máscara e o traçado do contorno proporcionais a isso, e não aos 12 MP de
// uma foto de celular.
const LADO_SAM = 1024;
// Tolerância da simplificação, em fração da diagonal da máscara: ~2 px numa
// imagem de 1000 px — abaixo do que a pessoa corrigiria à mão.
const EPS = 0.002;
const ALCA = 5;           // meio-lado da alça de vértice, em px

export const seg = {
  carga: null,            // Promise do SAM (modelo + processador), uma por aba
  situacao: "parado",     // carregando | codificando | pronto | indisponivel
  motivo: "",
  url: null,              // imagem cujos embeddings estão em `emb`
  emb: null,              // { emb, entradas, w, h }
  manual: false,          // desenhar à mão mesmo com o SAM no ar
  // { pontos: [{x,y,positivo}], pts: [[x,y]...] }: no SAM, `pontos` são os
  // cliques e `pts` o contorno que eles deram; à mão, `pts` são os vértices.
  rascunho: null,
  arrasto: null,
  ocupado: false, denovo: false,
};

export const usaSam = () => !seg.manual && seg.situacao === "pronto";

/** Esquece a imagem anterior: chamado no INÍCIO de abrir outra, antes de
 *  qualquer await — senão o rascunho e o estado de A aparecem sobre B (e
 *  aceitam clique) enquanto B carrega. */
export function esquecer() {
  seg.rascunho = null; seg.arrasto = null; seg.emb = null; seg.url = null;
  if (seg.situacao === "pronto" || seg.situacao === "codificando")
    seg.situacao = "parado";
}

/** O contorno do rascunho ainda é o dos cliques atuais? Com o decoder
 *  rodando (ou um clique na fila), `pts` é o do clique anterior. */
export const contornoEmDia = () => !seg.ocupado && !seg.denovo;

/** Texto curto do estado do SAM, para o topo do anotador. */
export function situacaoSam() {
  if (seg.manual) return "à mão (m volta ao SAM)";
  return { parado: "", carregando: "carregando o SAM…",
           codificando: "SAM lendo a imagem…", pronto: "SAM pronto",
           indisponivel: "SAM indisponível — à mão" }[seg.situacao] || "";
}

function carregarSam(aoMudar) {
  if (!seg.carga) {
    seg.situacao = "carregando"; aoMudar();
    seg.carga = (async () => {
      const t = await import(TRANSFORMERS);
      t.env.allowLocalModels = false;
      const modelo = await t.AutoModel.from_pretrained(MODELO_SAM,
                                                       { dtype: "q8", device: "wasm" });
      const proc = await t.AutoProcessor.from_pretrained(MODELO_SAM);
      return { modelo, proc, RawImage: t.RawImage };
    })();
    // Falhou (rede, CDN fora): a próxima imagem tenta de novo, em vez de a
    // aba inteira ficar sem SAM até recarregar.
    seg.carga.catch(() => { seg.carga = null; });
  }
  return seg.carga;
}

/** Prepara o SAM para a imagem aberta: o encoder roda uma vez por imagem
 *  (~2 s), e cada clique depois disso custa só o decoder (~100 ms). */
export async function prepararImagem(url, aoMudar) {
  seg.rascunho = null; seg.arrasto = null; seg.emb = null; seg.url = url;
  if (!url) return;
  let etapa = "modelo";
  try {
    const sam = await carregarSam(aoMudar);
    if (seg.url !== url) return;
    seg.situacao = "codificando"; aoMudar();
    etapa = "imagem";
    // O SAM precisa dos pixels. `url` é a rota `sam-image` da API (mesmo
    // endereço da tela) ou, se o parâmetro `sam_image_source` mandar, a URL
    // assinada do bucket — e aí é o CORS do bucket que libera este fetch. A
    // <img> da tela não passa pelo CORS: abre mesmo quando aqui falha.
    const r = await fetch(url, { mode: "cors" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const bmp = await createImageBitmap(await r.blob());
    const k = Math.min(1, LADO_SAM / Math.max(bmp.width, bmp.height));
    const w = Math.round(bmp.width * k), h = Math.round(bmp.height * k);
    const tela = new OffscreenCanvas(w, h);
    tela.getContext("2d").drawImage(bmp, 0, 0, w, h);
    etapa = "encoder";
    const imagem = await sam.RawImage.fromBlob(await tela.convertToBlob());
    const entradas = await sam.proc(imagem);
    const emb = await sam.modelo.get_image_embeddings(entradas);
    if (seg.url !== url) return;              // trocou de imagem no meio
    seg.emb = { emb, entradas, w, h };
    seg.situacao = "pronto"; seg.motivo = "";
  } catch (e) {
    if (seg.url !== url) return;
    seg.situacao = "indisponivel";
    const pelaApi = url.startsWith("/");
    seg.motivo = etapa === "imagem"
      ? (pelaApi
        ? `a API não conseguiu entregar a imagem (${e.message}). `
        : "o navegador não conseguiu ler os pixels direto do bucket (ele " +
          "precisa liberar CORS para este endereço, ou mude em Parâmetros " +
          "para ler pela API). ") + "Desenhe à mão: clique nos vértices."
      : `não consegui ${etapa === "modelo" ? "carregar" : "rodar"} o SAM ` +
        `(${e.message}). Desenhe à mão: clique nos vértices.`;
    console.error("SAM:", e);
  }
  aoMudar();
}

/** Roda o decoder com os cliques do rascunho e troca o contorno dele. Clique
 *  que chega com o decoder ocupado não se perde: marca `denovo` e a volta
 *  seguinte já usa todos os pontos. */
async function segmentar(aoMudar) {
  if (seg.ocupado) { seg.denovo = true; return; }
  seg.ocupado = true;
  try {
    do {
      seg.denovo = false;
      const r = seg.rascunho;
      if (!r || !seg.emb) return;
      const sam = await seg.carga;
      // trocou de imagem durante o await: `emb` já é de outra (ou nulo), e
      // isso não é falha do SAM para avisar
      if (seg.rascunho !== r || !seg.emb) return;
      const { emb, entradas, w, h } = seg.emb;
      const input_points = sam.proc.reshape_input_points(
        [[r.pontos.map((p) => [p.x * w, p.y * h])]],
        entradas.original_sizes, entradas.reshaped_input_sizes);
      const input_labels = sam.proc.image_processor.add_input_labels(
        [[r.pontos.map((p) => (p.positivo ? 1 : 0))]], input_points);
      const saida = await sam.modelo({ ...emb, input_points, input_labels });
      const masks = await sam.proc.post_process_masks(
        saida.pred_masks, entradas.original_sizes, entradas.reshaped_input_sizes);
      // Três hipóteses por prompt; fica a de maior IoU previsto.
      const iou = saida.iou_scores.data;
      let m = 0;
      for (let i = 1; i < iou.length; i++) if (iou[i] > iou[m]) m = i;
      const [, , H, W] = masks[0].dims;
      const px = masks[0].data.subarray(m * W * H, (m + 1) * W * H);
      const anel = maiorContorno(W, H, px);
      if (seg.rascunho === r) {
        r.pts = anel ? simplificar(anel, EPS * Math.hypot(W, H))
          .map(([x, y]) => [(x + 0.5) / W, (y + 0.5) / H]) : null;
      }
      aoMudar();
    } while (seg.denovo);
  } catch (e) {
    // Sem isto o erro sumia (as chamadas não esperam a Promise) e o
    // contorno velho seguia aceitável como se fosse dos cliques atuais.
    console.error("SAM decoder:", e);
    if (seg.rascunho) seg.rascunho.pts = null;
    seg.denovo = false;
    avisar(`o SAM falhou neste clique (${e.message}) — tente de novo ou ` +
           "desenhe à mão (m)", true);
    aoMudar();
  } finally { seg.ocupado = false; }
}

// --------------------------------------------------------- geometria --

/** Maior componente conexa da máscara e o contorno externo dela (Moore). */
function maiorContorno(W, H, px) {
  const rot = new Int32Array(W * H), fila = new Int32Array(W * H);
  let atual = 0, maior = 0, tamMaior = 0;
  for (let i = 0; i < W * H; i++) {
    if (!px[i] || rot[i]) continue;
    atual++; let ini = 0, fim = 0; fila[fim++] = i; rot[i] = atual;
    while (ini < fim) {
      const p = fila[ini++], x = p % W, y = (p / W) | 0;
      if (x > 0 && px[p - 1] && !rot[p - 1]) { rot[p - 1] = atual; fila[fim++] = p - 1; }
      if (x < W - 1 && px[p + 1] && !rot[p + 1]) { rot[p + 1] = atual; fila[fim++] = p + 1; }
      if (y > 0 && px[p - W] && !rot[p - W]) { rot[p - W] = atual; fila[fim++] = p - W; }
      if (y < H - 1 && px[p + W] && !rot[p + W]) { rot[p + W] = atual; fila[fim++] = p + W; }
    }
    if (fim > tamMaior) { tamMaior = fim; maior = atual; }
  }
  if (!maior) return null;
  const inicio = rot.indexOf(maior);
  const dentro = (x, y) => x >= 0 && y >= 0 && x < W && y < H && rot[y * W + x] === maior;
  const viz = [[1, 0], [1, 1], [0, 1], [-1, 1], [-1, 0], [-1, -1], [0, -1], [1, -1]];
  const sx = inicio % W, sy = (inicio / W) | 0;
  const pts = [[sx, sy]];
  let x = sx, y = sy, dir = 7, passos = 0;
  do {
    let achou = false;
    for (let k = 0; k < 8; k++) {
      const d = (dir + 6 + k) % 8, nx = x + viz[d][0], ny = y + viz[d][1];
      if (dentro(nx, ny)) { x = nx; y = ny; dir = d; achou = true; break; }
    }
    if (!achou) break;
    pts.push([x, y]);
  } while ((x !== sx || y !== sy) && ++passos < 4 * W * H);
  return pts;
}

/** Contorno fechado → menos vértices. Parte o anel no ponto mais longe do
 *  início: Douglas–Peucker contra um segmento de comprimento zero (início =
 *  fim) descartaria tudo — foi o defeito do protótipo. */
function simplificar(anel, eps) {
  if (anel.length < 4) return anel;
  const [x0, y0] = anel[0];
  let longe = 0, dmax = -1;
  anel.forEach(([x, y], i) => {
    const d = Math.hypot(x - x0, y - y0); if (d > dmax) { dmax = d; longe = i; }
  });
  const ida = linha(anel.slice(0, longe + 1), eps);
  const volta = linha([...anel.slice(longe), anel[0]], eps);
  return [...ida, ...volta.slice(1, -1)];
}

function linha(pts, eps) {
  if (pts.length < 3) return pts;
  const manter = new Uint8Array(pts.length);
  manter[0] = manter[pts.length - 1] = 1;
  const pilha = [[0, pts.length - 1]];
  while (pilha.length) {
    const [a, b] = pilha.pop();
    const [ax, ay] = pts[a], [bx, by] = pts[b];
    const dx = bx - ax, dy = by - ay, n = Math.hypot(dx, dy) || 1;
    let dmax = 0, idx = -1;
    for (let i = a + 1; i < b; i++) {
      const d = Math.abs(dy * (pts[i][0] - ax) - dx * (pts[i][1] - ay)) / n;
      if (d > dmax) { dmax = d; idx = i; }
    }
    if (dmax > eps) { manter[idx] = 1; pilha.push([a, idx], [idx, b]); }
  }
  return pts.filter((_, i) => manter[i]);
}

function dentroDe(x, y, pts) {
  let dentro = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const [xi, yi] = pts[i], [xj, yj] = pts[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi)
      dentro = !dentro;
  }
  return dentro;
}

function area(pts) {
  let s = 0;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++)
    s += pts[j][0] * pts[i][1] - pts[i][0] * pts[j][1];
  return Math.abs(s) / 2;
}

const emPx = ([x, y], cv) => [x * cv.width, y * cv.height];

function verticeEm(p, b, cv) {
  return b.pts.findIndex((v) => {
    const [vx, vy] = emPx(v, cv);
    return Math.abs(p.x - vx) <= ALCA + 2 && Math.abs(p.y - vy) <= ALCA + 2;
  });
}

/** Aresta do polígono mais perto de `p` (px), se a menos de 8 px: onde um
 *  duplo clique insere vértice. Devolve o índice de inserção e o ponto. */
function arestaEm(p, b, cv) {
  let melhor = null;
  b.pts.forEach((v, i) => {
    const [ax, ay] = emPx(v, cv), [bx, by] = emPx(b.pts[(i + 1) % b.pts.length], cv);
    const dx = bx - ax, dy = by - ay, l2 = dx * dx + dy * dy || 1;
    const t = Math.max(0, Math.min(1, ((p.x - ax) * dx + (p.y - ay) * dy) / l2));
    const qx = ax + t * dx, qy = ay + t * dy, d = Math.hypot(p.x - qx, p.y - qy);
    if (d < 8 && (!melhor || d < melhor.d))
      melhor = { d, indice: i + 1, pt: [qx / cv.width, qy / cv.height] };
  });
  return melhor;
}

const clamp01 = (v) => Math.max(0, Math.min(1, v));

// ---------------------------------------------------------- interação --

/** Clique no canvas. `p` em px do canvas; `ctx` é o que o anotador passa:
 *  { anot, cv, aoMudar } — `aoMudar` redesenha. */
export function apertar(ev, p, { anot, cv, aoMudar }) {
  const x = clamp01(p.x / cv.width), y = clamp01(p.y / cv.height);
  const negativo = ev.button === 2 || ev.shiftKey;
  const r = seg.rascunho;

  if (r && r.sam) {                         // mais um ponto para o SAM
    r.pontos.push({ x, y, positivo: !negativo });
    aoMudar(); segmentar(aoMudar);
    return;
  }
  if (r) {                                  // à mão: mais um vértice
    if (ev.button === 2) return;
    const [x0, y0] = emPx(r.pts[0], cv);
    // clicar de volta no primeiro vértice fecha o polígono
    if (r.pts.length >= 3 && Math.hypot(p.x - x0, p.y - y0) <= ALCA + 3) {
      aceitar(anot, aoMudar); return;
    }
    r.pts.push([x, y]); aoMudar();
    return;
  }
  const sel = anot.sel >= 0 ? anot.boxes[anot.sel] : null;
  if (sel && sel.pts) {
    const v = verticeEm(p, sel, cv);
    if (v >= 0) {
      if (ev.button === 2 || ev.altKey) {     // apagar o vértice
        if (sel.pts.length > 3) { sel.pts.splice(v, 1); anot.sujo = true; }
        aoMudar(); return;
      }
      seg.arrasto = { vertice: v }; return;
    }
  }
  if (ev.button === 2) return;
  // Perto da aresta do selecionado, pelo lado de fora: é o primeiro clique de
  // um duplo clique que vai inserir vértice, não o começo de outro contorno.
  if (sel && sel.pts && arestaEm(p, sel, cv)) return;
  for (let i = anot.boxes.length - 1; i >= 0; i--) {
    const b = anot.boxes[i];
    if (b.pts && dentroDe(x, y, b.pts) && !ev.shiftKey) {
      anot.sel = i;
      // `px0`: o polígono só anda depois de 3 px de arrasto. Sem isso o
      // tremor entre os dois cliques de um duplo clique na aresta o movia.
      seg.arrasto = { mover: [x, y], px0: [p.x, p.y], solto: false };
      aoMudar(); return;
    }
  }
  // área vazia: começa um contorno novo
  anot.sel = -1;
  seg.rascunho = usaSam()
    ? { sam: true, pontos: [{ x, y, positivo: true }], pts: null }
    : { sam: false, pts: [[x, y]] };
  aoMudar();
  if (seg.rascunho.sam) segmentar(aoMudar);
}

export function mover(p, { anot, cv, aoMudar }) {
  const a = seg.arrasto, b = anot.sel >= 0 ? anot.boxes[anot.sel] : null;
  if (!a || !b || !b.pts) return false;
  const x = clamp01(p.x / cv.width), y = clamp01(p.y / cv.height);
  if (a.vertice !== undefined) {
    b.pts[a.vertice] = [x, y];
  } else {
    if (!a.solto && Math.hypot(p.x - a.px0[0], p.y - a.px0[1]) < 3) return true;
    a.solto = true;
    // move o polígono inteiro, sem deixar vértice sair da imagem
    let dx = x - a.mover[0], dy = y - a.mover[1];
    for (const [vx, vy] of b.pts) {
      dx = Math.max(-vx, Math.min(1 - vx, dx));
      dy = Math.max(-vy, Math.min(1 - vy, dy));
    }
    b.pts = b.pts.map(([vx, vy]) => [vx + dx, vy + dy]);
    a.mover = [a.mover[0] + dx, a.mover[1] + dy];
  }
  anot.sujo = true; aoMudar();
  return true;
}

export function soltar() { seg.arrasto = null; }

/** Cursor conforme o que está sob o ponteiro. */
export function cursor(p, { anot, cv }) {
  const sel = anot.sel >= 0 ? anot.boxes[anot.sel] : null;
  if (!seg.rascunho && sel && sel.pts && verticeEm(p, sel, cv) >= 0) return "grab";
  return "crosshair";
}

/** Duplo clique numa aresta do selecionado insere um vértice ali. */
export function duploClique(p, { anot, cv, aoMudar }) {
  const sel = anot.sel >= 0 ? anot.boxes[anot.sel] : null;
  if (seg.rascunho || !sel || !sel.pts) return;
  const a = arestaEm(p, sel, cv);
  if (!a) return;
  sel.pts.splice(a.indice, 0, a.pt);
  anot.sujo = true; aoMudar();
}

/** O contorno do rascunho vira anotação, na classe atual. */
export function aceitar(anot, aoMudar) {
  const r = seg.rascunho;
  const pts = r && r.pts;
  if (r && r.sam && !contornoEmDia()) return "o SAM ainda está calculando — um instante";
  if (!pts || pts.length < 3 || area(pts) < 1e-6) {
    return "o contorno ainda não tem área — clique mais pontos";
  }
  anot.boxes.push({ cls: anot.classe,
                    pts: pts.map(([x, y]) => [+x.toFixed(6), +y.toFixed(6)]) });
  anot.sel = anot.boxes.length - 1;
  anot.sujo = true;
  seg.rascunho = null;
  aoMudar();
  return null;
}

/** Teclas próprias da segmentação. Devolve true se consumiu a tecla. */
export function tecla(ev, anot, aoMudar, avisar) {
  if (ev.key === "Enter" && seg.rascunho) {
    ev.preventDefault();
    const erro = aceitar(anot, aoMudar);
    if (erro) avisar(erro, true);
    return true;
  }
  if (ev.key === "Escape" && seg.rascunho) {
    seg.rascunho = null; aoMudar(); return true;
  }
  if (ev.key === "Backspace" && seg.rascunho) {
    ev.preventDefault();
    const r = seg.rascunho;
    if (r.sam) { r.pontos.pop(); if (!r.pontos.length) seg.rascunho = null;
                 else segmentar(aoMudar); }
    else { r.pts.pop(); if (!r.pts.length) seg.rascunho = null; }
    aoMudar(); return true;
  }
  if (ev.key.toLowerCase() === "m") {
    seg.manual = !seg.manual; seg.rascunho = null; aoMudar(); return true;
  }
  return false;
}

// ---------------------------------------------------------- desenho --

export function desenhar(cx, cv, anot) {
  anot.boxes.forEach((b, i) => {
    if (!b.pts) return;
    const selecionado = i === anot.sel;
    caminho(cx, b.pts, cv);
    cx.fillStyle = cor(b.cls) + (selecionado ? "55" : "33");
    cx.fill();
    cx.lineWidth = selecionado ? 3 : 2;
    cx.strokeStyle = cor(b.cls);
    cx.stroke();
    if (anot.nomes) rotulo(cx, b, cv, anot);
    if (selecionado) for (const v of b.pts) alca(cx, emPx(v, cv), cor(b.cls));
  });
  const r = seg.rascunho;
  if (!r) return;
  const c = cor(anot.classe);
  if (r.pts && r.pts.length) {
    caminho(cx, r.pts, cv, !r.sam);
    if (r.sam) { cx.fillStyle = c + "44"; cx.fill(); }
    cx.setLineDash([5, 4]); cx.lineWidth = 2; cx.strokeStyle = c; cx.stroke();
    cx.setLineDash([]);
    if (!r.sam) for (const v of r.pts) alca(cx, emPx(v, cv), c);
  }
  if (r.sam) for (const p of r.pontos) {
    const [x, y] = emPx([p.x, p.y], cv);
    cx.beginPath(); cx.arc(x, y, 6, 0, 2 * Math.PI);
    cx.fillStyle = p.positivo ? "#2fbf71" : "#e5484d"; cx.fill();
    cx.lineWidth = 2; cx.strokeStyle = "#fff"; cx.stroke();
  }
}

function caminho(cx, pts, cv, aberto = false) {
  cx.beginPath();
  pts.forEach((v, i) => {
    const [x, y] = emPx(v, cv);
    if (i) cx.lineTo(x, y); else cx.moveTo(x, y);
  });
  if (!aberto) cx.closePath();
}

function alca(cx, [x, y], c) {
  cx.fillStyle = "#0d1217"; cx.strokeStyle = c; cx.lineWidth = 2;
  cx.fillRect(x - ALCA, y - ALCA, ALCA * 2, ALCA * 2);
  cx.strokeRect(x - ALCA, y - ALCA, ALCA * 2, ALCA * 2);
}

function rotulo(cx, b, cv, anot) {
  const nome = (anot.classes[b.cls] || {}).nome || String(b.cls);
  // no vértice mais alto: é onde o nome menos cobre o próprio contorno
  const [x, y] = emPx(b.pts.reduce((a, v) => (v[1] < a[1] ? v : a)), cv);
  cx.font = "600 11px ui-monospace, monospace";
  const larg = cx.measureText(nome).width + 8;
  const acima = y >= 15;
  cx.fillStyle = cor(b.cls);
  cx.fillRect(x, acima ? y - 15 : y, larg, 15);
  cx.fillStyle = "#0d1217";
  cx.fillText(nome, x + 4, acima ? y - 4 : y + 11);
}
