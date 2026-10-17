/* Segmentação no anotador, em duas etapas. No contorno, a principal sai de
 * um clique do SAM, se ajusta vértice a vértice e trava. Nos recortes, ela
 * se divide em pedaços que herdam a borda dela. Sem SAM (rede, CORS do
 * bucket), o contorno se desenha à mão, clicando nos vértices.
 *
 * Só importa de `nucleo.js` e `geometria.js`: quem conhece o estado da tela
 * (`anot`) é o `anotador.js`, que chama estas funções passando o que elas
 * precisam. */

import { area, caixaDe, classePorSobreposicao, dentroDe, doQuadro, fatiar, linha,
         paraQuadro, pontoDentro, recortarPor } from "./geometria.js";
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
  etapa: "contorno",      // contorno | recortes
  editando: null,         // a principal destravada (no máximo uma)
  editandoAntes: null,    // o contorno dela ao destravar, em JSON
  ativa: null,            // a principal que os recortes dividem
  ferramenta: "fatiar",   // fatiar | desenhar
  traco: null,            // { px: [[x,y]...] } do arrasto nos recortes
  desfazer: [],
  pc: null,               // polygon-clipping, quando carregado
  // Por principal, os cortes vivos: { base, linhas, gabarito?, espelho? }.
  // `base` são os pedaços que ela tinha antes da primeira linha (vazio se
  // foi cortada do zero); os pedaços de agora são `base` + cada linha, em
  // ordem. É isso que deixa arrastar um ponto da linha e refazer tudo.
  cortes: new Map(),
  arrastoLinha: null,     // { linha, vertice, referencia }
  arrastoLaco: null,      // { peca, vertice }: vértice do laço selecionado
  escala: [1000, 1000],   // a imagem em 1000 de largura, para o mínimo do fatiar
  gabaritos: new Map(),   // índice da classe principal → { lines, seeds }
};

export const usaSam = () => !seg.manual && seg.situacao === "pronto";

/** Esquece a imagem anterior: chamado no INÍCIO de abrir outra, antes de
 *  qualquer await — senão o rascunho e o estado de A aparecem sobre B (e
 *  aceitam clique) enquanto B carrega. */
export function esquecer() {
  seg.rascunho = null; seg.arrasto = null; seg.emb = null; seg.url = null;
  seg.etapa = "contorno"; seg.editando = null; seg.ativa = null;
  seg.traco = null; seg.desfazer = [];
  seg.cortes = new Map(); seg.arrastoLinha = null; seg.arrastoLaco = null;
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

// Embeddings já calculados (ou em cálculo), por sample: a próxima imagem é
// codificada enquanto se anota a atual, e voltar uma imagem não recalcula.
// Por id e não por URL: no modo "bucket" a URL assinada muda a cada pedido.
// ~4 MB cada (256×64×64 float32), por isso poucos.
const cache = new Map();
const MAX_CACHE = 3;
// Um encoder por vez: o wasm divide a mesma CPU, e a pré-carga correndo junto
// atrasaria justamente a imagem que está na tela.
let fila = Promise.resolve();

function codificar(chave, url, aoMudar) {
  if (cache.has(chave)) return cache.get(chave);
  const trabalho = fila.then(async () => {
    let etapa = "modelo";
    try {
      const sam = await carregarSam(aoMudar);
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
      return { emb, entradas, w, h };
    } catch (e) { e.etapa = etapa; throw e; }
  });
  fila = trabalho.catch(() => {});
  // `pronta`: a pré-carga ainda rodando também está no cache, e quem abre a
  // imagem nesse meio-tempo precisa ver "codificando", não "parado"
  trabalho.then(() => { trabalho.pronta = true; }, () => {});
  cache.set(chave, trabalho);
  // falha não fica guardada: reabrir a imagem tenta de novo
  trabalho.catch(() => { if (cache.get(chave) === trabalho) cache.delete(chave); });
  while (cache.size > MAX_CACHE) cache.delete(cache.keys().next().value);
  return trabalho;
}

/** Prepara o SAM para a imagem aberta: o encoder roda uma vez por imagem
 *  (~2 s), e cada clique depois disso custa só o decoder (~100 ms). Se a
 *  pré-carga já a codificou, é instantâneo. */
export async function prepararImagem(url, aoMudar, chave = url) {
  seg.rascunho = null; seg.arrasto = null; seg.emb = null; seg.url = chave;
  if (!url) return;
  try {
    const pronta = cache.get(chave)?.pronta;
    if (!pronta) { seg.situacao = seg.carga ? "codificando" : "carregando"; aoMudar(); }
    const emb = await codificar(chave, url, () => {
      if (seg.url === chave) { seg.situacao = "carregando"; aoMudar(); }
    });
    if (seg.url !== chave) return;              // trocou de imagem no meio
    seg.emb = emb;
    seg.situacao = "pronto"; seg.motivo = "";
  } catch (e) {
    if (seg.url !== chave) return;
    seg.situacao = "indisponivel";
    const pelaApi = url.startsWith("/");
    seg.motivo = e.etapa === "imagem"
      ? (pelaApi
        ? `a API não conseguiu entregar a imagem (${e.message}). `
        : "o navegador não conseguiu ler os pixels direto do bucket (ele " +
          "precisa liberar CORS para este endereço, ou mude em Parâmetros " +
          "para ler pela API). ") + "Desenhe à mão: clique nos vértices."
      : `não consegui ${e.etapa === "modelo" ? "carregar" : "rodar"} o SAM ` +
        `(${e.message}). Desenhe à mão: clique nos vértices.`;
    console.error("SAM:", e);
  }
  aoMudar();
}

/** Codifica uma imagem que ainda não está na tela (a próxima), sem mexer no
 *  estado da atual. Só com o SAM já carregado: quem nunca o usou não paga. */
export function preCarregar(chave, url) {
  if (!url || !seg.carga || cache.has(chave)) return;
  codificar(chave, url, () => {}).catch(() => {});
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

// ------------------------------------------------- principal e recortes --
//
// A imagem tem uma ou mais principais (classe sem "Dentro de") e os
// recortes de cada uma. O que se grava é a lista plana `{cls, pts}`; quem é
// recorte de quem vive só aqui (`b.dono`), refeito ao abrir a imagem.
//
// Etapa "contorno": cada clique é o SAM, e a principal aceita fica TRAVADA —
// é o que permite marcar a segunda ao lado (ou por cima) sem arrastar a
// primeira. Só uma por vez fica destravada (`seg.editando`): a recém-aceita,
// para ajustar os vértices, ou a que se mandou editar pela lista.
// Etapa "recortes": a principal ativa não se mexe; arrastar fatia ou laça
// dentro dela, clicar escolhe o pedaço.

const RECORTE = "https://cdn.jsdelivr.net/npm/polygon-clipping@0.15.7/+esm";
let cargaRecorte = null;

/** A biblioteca de recorte, uma vez por aba. Sem ela (CDN fora) a etapa de
 *  recortes não abre; o contorno principal segue funcionando. */
export function carregarRecorte() {
  if (!cargaRecorte) {
    cargaRecorte = import(RECORTE).then((m) => (seg.pc = m.default));
    cargaRecorte.catch(() => { cargaRecorte = null; });
  }
  return cargaRecorte;
}

export const ehPrincipal = (b, anot) => b.cls !== null && anot.dentroDe[b.cls] == null;
const filhasDe = (cls, anot) => anot.dentroDe.flatMap((d, i) => (d === cls ? [i] : []));
const recortesDe = (p, anot) => anot.boxes.filter((b) => b.dono === p);
const principais = (anot) => anot.boxes.filter((b) => ehPrincipal(b, anot));

/** As classes que a lista lateral oferece: as principais no contorno, as
 *  "dentro de" a ativa nos recortes. Índices do schema, em ordem. */
export function classesVisiveis(anot) {
  if (seg.etapa === "recortes" && seg.ativa) return filhasDe(seg.ativa.cls, anot);
  return anot.dentroDe.flatMap((d, i) => (d == null ? [i] : []));
}

export const temRecortes = (anot) => anot.dentroDe.some((d) => d != null);

/** Liga cada recorte à principal que o contém. Chamado ao abrir a imagem e
 *  ao receber o pré-tag — a anotação gravada não diz quem é dono de quem. */
export function prepararCaixas(anot) {
  const ps = principais(anot);
  for (const b of anot.boxes) {
    if (ehPrincipal(b, anot)) continue;
    // Vértices puxados um pouco para dentro do pedaço: o recorte que herdou
    // a borda tem vértice EM CIMA do contorno, e aí o ponto-dentro decide
    // por arredondamento.
    const n = b.pts.length;
    const cx = b.pts.reduce((s, v) => s + v[0], 0) / n;
    const cy = b.pts.reduce((s, v) => s + v[1], 0) / n;
    const amostra = b.pts.map(([x, y]) => [x + (cx - x) * 0.05, y + (cy - y) * 0.05]);
    let melhor = null, mais = 0;
    for (const p of ps) {
      if (p.cls !== anot.dentroDe[b.cls]) continue;
      const k = amostra.filter(([x, y]) => dentroDe(x, y, p.pts)).length;
      if (k > mais) { mais = k; melhor = p; }
    }
    b.dono = melhor;
  }
}

/** O que impede gravar, ou nulo. */
export function pendencia(anot) {
  if (seg.rascunho) return "há um contorno em rascunho — Enter aceita, Esc descarta";
  // A API confere o índice, não o "dentro de": classe de outra principal
  // passaria e iria para o treino.
  const errada = anot.boxes.find((b) => b.dono && b.cls !== null &&
                                         anot.dentroDe[b.cls] !== b.dono.cls);
  if (errada) return `um recorte tem a classe "${(anot.classes[errada.cls] || {}).nome}", ` +
                     "que não está dentro desta principal — troque a classe dele";
  const sem = anot.boxes.filter((b) => b.cls === null).length;
  if (sem) return `${sem} pedaço(s) sem classe — clique no pedaço e escolha a ` +
                  "classe, ou apague (d)";
  return null;
}

/** Leva a tela até o pedaço sem classe que barrou a gravação: na lasca que
 *  o corte deixa numa reentrância, o aviso sozinho não diz onde ela está. */
export function apontarPendencia(anot) {
  const i = anot.boxes.findIndex((b) => b.cls === null);
  if (i < 0 || seg.rascunho || !seg.pc) return;
  const b = anot.boxes[i];
  if (!travar(anot)) return;
  if (b.dono) {
    seg.etapa = "recortes"; seg.ativa = b.dono; seg.traco = null;
    // a classe atual tem de ser da nova ativa, senão o próximo laço sai com
    // classe de outra principal
    anot.classe = filhasDe(b.dono.cls, anot)[0];
  }
  anot.sel = anot.boxes.indexOf(b);
}

/** A lista que vai para a API: só `{cls, pts}`, e o pedaço com vértice que
 *  o recorte deixou um fio fora da imagem volta para dentro. */
export function paraGravar(anot) {
  return anot.boxes.map((b) => ({
    cls: b.cls,
    pts: b.pts.map(([x, y]) => [+clamp01(x).toFixed(6), +clamp01(y).toFixed(6)]),
  }));
}

/** Trava a principal em edição e recorta os recortes dela pelo contorno
 *  novo — o que ficou de fora some. Onde o contorno cresceu fica um vão sem
 *  pedaço: o próximo corte que passar por ali o divide. */
export function travar(anot) {
  const p = seg.editando;
  if (!p) return true;
  // Contorno intacto (abriu "editar" e não mexeu): não há o que recortar, e
  // marcar `sujo` faria a navegação gravar uma anotação que ninguém alterou.
  if (!recortesDe(p, anot).length || JSON.stringify(p.pts) === seg.editandoAntes) {
    seg.editando = null;
    return true;
  }
  if (!seg.pc) {
    // Fica em edição: soltar aqui deixaria gravar os pedaços sem o recorte
    // novo, e depois não haveria mais como saber quem recortar.
    carregarRecorte().catch(() => {});
    avisar("a ferramenta de recorte não carregou — os recortes não podem " +
           "ser ajustados ao contorno novo. Tente de novo em um instante.", true);
    return false;
  }
  seg.editando = null;
  const c = seg.cortes.get(p);
  const referencia = fatias(p, anot);
  anot.boxes = anot.boxes.flatMap((b) => (b.dono !== p || (c && !b.laco) ? [b]
    : recortarPor(seg.pc, b.pts, p.pts).map((pts) => ({ ...b, pts }))));
  if (c) {
    // Com as linhas guardadas, o contorno novo é cortado de novo por elas:
    // onde ele cresceu, o pedaço cresce junto, em vez de ficar um vão.
    c.base = c.base.flatMap((b) => recortarPor(seg.pc, b.pts, p.pts).map((pts) => ({ ...b, pts })));
    refazer(p, anot, referencia);
  }
  anot.sel = anot.boxes.indexOf(p);
  anot.sujo = true;
  return true;
}

/** Começa a editar `b`, lembrando o contorno de antes (ver `travar`). */
function abrirEdicao(b) {
  seg.editando = b;
  seg.editandoAntes = JSON.stringify(b.pts);
}

/** Destrava a principal `i` (pela lista lateral) para mexer nos vértices. */
export function editar(anot, i) {
  const b = anot.boxes[i];
  if (!b || !ehPrincipal(b, anot) || seg.rascunho) return;
  if (!travar(anot)) return;
  seg.etapa = "contorno"; seg.ativa = null; seg.traco = null;
  abrirEdicao(b);
  anot.sel = anot.boxes.indexOf(b);
  anot.classe = b.cls;
}

/** Troca de etapa. Devolve o motivo de não ter trocado, ou nulo. */
export function mudarEtapa(etapa, anot) {
  if (etapa === seg.etapa) return null;
  if (seg.rascunho) return "há um contorno em rascunho — Enter aceita, Esc descarta";
  seg.traco = null;
  if (etapa === "contorno") {
    seg.etapa = "contorno"; seg.ativa = null; anot.sel = -1;
    anot.classe = classesVisiveis(anot)[0] ?? 0;
    return null;
  }
  if (!seg.pc) {
    carregarRecorte().catch(() => {});
    return "a ferramenta de recorte ainda está carregando (ou a rede falhou) — " +
           "tente de novo em um instante";
  }
  const ps = principais(anot);
  if (!ps.length) return "marque primeiro o contorno principal";
  const com = ps.filter((p) => filhasDe(p.cls, anot).length);
  if (!com.length) {
    return "nenhuma classe está \"dentro de\" " +
           [...new Set(ps.map((p) => (anot.classes[p.cls] || {}).nome))].join(", ") +
           " — crie as classes de recorte no projeto";
  }
  if (!travar(anot)) return "a principal em edição ainda não pôde travar";
  const sel = anot.boxes[anot.sel];
  const alvo = sel && (ehPrincipal(sel, anot) ? sel : sel.dono);
  seg.ativa = com.includes(alvo) ? alvo : com[0];
  seg.etapa = "recortes"; seg.desfazer = [];
  anot.sel = -1;
  anot.classe = filhasDe(seg.ativa.cls, anot)[0];
  return null;
}

/** Troca de ferramenta solta o pedaço selecionado: senão o número que se
 *  aperta em seguida, para escolher a classe do próximo laço, reclassifica
 *  o pedaço que ficou selecionado. */
export function mudarFerramenta(f, anot) {
  seg.ferramenta = f; seg.traco = null; anot.sel = -1;
}

/** Ponto de desfazer dos recortes: a lista e a classe de cada item. Basta
 *  isso porque cortar e laçar trocam objetos em vez de mexer neles, e a
 *  única mutação in loco nesta etapa é a classe. */
function guardar(anot) {
  const cortes = new Map([...seg.cortes].map(([p, c]) =>
    [p, { ...c, linhas: c.linhas.map((l) => l.map((v) => [...v])) }]));
  seg.desfazer.push({ itens: anot.boxes.map((b) => [b, b.cls]), cortes });
  if (seg.desfazer.length > 50) seg.desfazer.shift();
}

function desfazer(anot) {
  const s = seg.desfazer.pop();
  if (!s) return false;
  anot.boxes = s.itens.map(([b, cls]) => { b.cls = cls; return b; });
  seg.cortes = s.cortes;
  // Ctrl+Z com a alça ainda segura: o índice da linha arrastada pode não
  // existir mais nos cortes restaurados
  seg.arrastoLinha = null; seg.arrastoLaco = null; seg.traco = null;
  anot.sel = -1; anot.sujo = true;
  if (seg.ativa && !anot.boxes.includes(seg.ativa)) seg.ativa = null;
  return true;
}

/** Classe escolhida na lista (clique ou número) com algo selecionado:
 *  principal só troca para outra principal, e sem recortes (eles deixariam
 *  de estar "dentro de" quem os recorta); pedaço, para uma irmã. */
export function reclassificar(anot, cls) {
  // No meio de um arrasto o ponto de desfazer guardaria o objeto que o
  // arrasto continua mexendo, e o Ctrl+Z voltaria uma geometria de depois.
  if (seg.arrastoLaco || seg.arrastoLinha) return null;
  const b = anot.boxes[anot.sel];
  if (!b || b.cls === cls) return null;
  if (ehPrincipal(b, anot)) {
    if (seg.etapa !== "contorno" || anot.dentroDe[cls] != null) return null;
    if (recortesDe(b, anot).length)
      return "esta principal tem recortes — apague-os antes de trocar a classe";
  } else {
    // a irmã é pela dona do pedaço, não pela ativa: pela lista dá para
    // selecionar o pedaço de outra principal
    const dona = b.dono ? b.dono.cls : anot.dentroDe[b.cls];
    if (seg.etapa !== "recortes" || !filhasDe(dona, anot).includes(cls)) return null;
    guardar(anot);
  }
  b.cls = cls; anot.sujo = true;
  return null;
}

/** Apaga o item `i`; principal leva os recortes junto (sem ela, eles não
 *  teriam o que recortar). */
export function apagar(anot, i) {
  const b = anot.boxes[i];
  if (!b) return;
  const principal = ehPrincipal(b, anot);
  const n = principal ? recortesDe(b, anot).length : 0;
  if (n && !confirm(`Apagar o contorno e os ${n} recorte(s) dele?`)) return;
  // Principal apagada não entra no Ctrl+Z, e o histórico zera: um ponto
  // guardado antes a traria de volta sem ninguém pedir.
  if (principal) seg.desfazer = [];
  else if (seg.etapa === "recortes") guardar(anot);
  const some = principal ? new Set([b, ...recortesDe(b, anot)]) : new Set([b]);
  // Apagar a principal leva os cortes dela; apagar uma fatia os congela —
  // refazer pelas linhas traria de volta o pedaço que se mandou apagar.
  if (principal) seg.cortes.delete(b);
  else if (!b.laco && b.dono) seg.cortes.delete(b.dono);
  anot.boxes = anot.boxes.filter((x) => !some.has(x));
  if (some.has(seg.editando)) seg.editando = null;
  if (some.has(seg.ativa)) { seg.ativa = null; seg.etapa = "contorno"; }
  anot.sel = -1; anot.sujo = true;
}

// ---------------------------------------------------------- gabarito --
//
// Gabarito = as linhas dos cortes de uma banda bem recortada e uma semente
// por pedaço (o ponto que diz a classe), no quadro da caixa da principal.
// Aplicado noutra banda, as linhas vão para a caixa dela, os cortes rodam
// sobre o contorno dela (a borda vem sempre do SAM da própria imagem) e cada
// pedaço pega a classe da semente que cai nele. Depois, as linhas ficam
// vivas para o ajuste fino.

export function definirGabaritos(lista) {
  seg.gabaritos = new Map(lista.map((g) => [g.class_index, g]));
}

/** O que gravar como gabarito da classe da ativa: `{ classe, corpo }`, ou
 *  `{ erro }` dizendo o que falta.
 *
 *  Fatias cortadas agora, do zero, vão como linhas + sementes: no gabarito
 *  aplicado elas continuam linhas vivas. O resto — laços, e os recortes
 *  que vieram de uma anotação salva (deles não há linha) — vai como região
 *  com classe, e volta como laço cortado pela borda da banda nova. */
export function montarGabarito(anot) {
  const a = seg.ativa;
  if (!a) return { erro: "clique na principal cujo recorte vai virar gabarito" };
  const c = seg.cortes.get(a);
  const doDono = anot.boxes.filter((b) => b.dono === a);
  if (!doDono.length)
    return { erro: "recorte esta principal (Fatiar ou Desenhar) antes de salvar o gabarito" };
  if (doDono.some((b) => b.cls === null))
    return { erro: "dê classe a todos os pedaços antes de salvar o gabarito" };
  const vivas = c && !c.base.length && c.linhas.length;
  const pecas = vivas ? fatias(a, anot) : [];
  const regioes = doDono.filter((b) => !pecas.includes(b));
  const caixa = caixaDe(a.pts);
  const q = (v) => paraQuadro(v, caixa).map((n) => +n.toFixed(5));
  return { classe: a.cls,
           corpo: { lines: vivas ? c.linhas.map((l) => l.map(q)) : [],
                    seeds: pecas.map((b) => ({ pt: q(pontoDentro(b.pts)), cls: b.cls })),
                    shapes: regioes.map((b) => ({ cls: b.cls, pts: b.pts.map(q) })) } };
}

/** Aplica o gabarito da classe na principal ativa. Devolve o motivo de não
 *  ter aplicado, ou nulo. Os recortes que ela tinha (fatias e laços) são
 *  trocados pelos do gabarito. */
export function aplicarGabarito(anot, espelho = false) {
  const a = seg.ativa;
  if (!a) return "clique na principal que quer recortar";
  if (!seg.pc) return "a ferramenta de recorte ainda está carregando";
  const g = seg.gabaritos.get(a.cls);
  if (!g) {
    return `não há gabarito para ${(anot.classes[a.cls] || {}).nome} — recorte ` +
           "uma bem feita e use \"Salvar gabarito\"";
  }
  const c = seg.cortes.get(a);
  if (!(c && c.gabarito) && recortesDe(a, anot).length &&
      !confirm("Trocar os recortes desta principal pelos do gabarito?")) return null;
  guardar(anot);
  const caixa = caixaDe(a.pts);
  anot.boxes = anot.boxes.filter((b) => b.dono !== a);
  seg.cortes.set(a, { base: [], gabarito: true, espelho,
                      linhas: g.lines.map((l) => l.map((v) => doQuadro(v, caixa, espelho))) });
  refazer(a, anot, []);
  const pecas = fatias(a, anot);
  const sementes = g.seeds.map((sem) => ({ cls: sem.cls, pt: doQuadro(sem.pt, caixa, espelho) }));
  for (const sem of sementes) {
    const alvo = pecas.find((b) => b.cls === null && dentroDe(sem.pt[0], sem.pt[1], b.pts));
    if (alvo) alvo.cls = sem.cls;
  }
  // A semente que cai fora (a perna desta banda é mais fina que a do
  // gabarito, e o ponto ficou no fundo) não deixa o pedaço sem classe: vale
  // a mais próxima, preferindo classe que ainda não foi dada a ninguém.
  const usadas = new Set(pecas.map((b) => b.cls).filter((x) => x !== null));
  for (const b of pecas.filter((x) => x.cls === null)) {
    const [px, py] = pontoDentro(b.pts);
    let melhor = null, nota = Infinity;
    for (const sem of sementes) {
      const d = Math.hypot(sem.pt[0] - px, sem.pt[1] - py) + (usadas.has(sem.cls) ? 10 : 0);
      if (d < nota) { nota = d; melhor = sem; }
    }
    if (melhor) { b.cls = melhor.cls; usadas.add(melhor.cls); }
  }
  for (const r of g.shapes || []) {
    const pts = r.pts.map((v) => doQuadro(v, caixa, espelho));
    for (const anel of recortarPor(seg.pc, pts, a.pts))
      anot.boxes.push({ cls: r.cls, pts: anel, dono: a, laco: true });
  }
  anot.sel = -1; anot.sujo = true;
  return null;
}

/** Reaplica o gabarito virado na horizontal (o ajuste fino feito se perde). */
export function espelhar(anot) {
  const c = seg.ativa && seg.cortes.get(seg.ativa);
  if (!c || !c.gabarito) return "espelhar vale para o gabarito aplicado (t)";
  return aplicarGabarito(anot, !c.espelho);
}

// ---------------------------------------------------------- interação --

/** Clique no canvas. `p` em px do canvas; `ctx` é o que o anotador passa:
 *  { anot, cv, aoMudar } — `aoMudar` redesenha. */
export function apertar(ev, p, { anot, cv, aoMudar }) {
  seg.escala = [1000, (1000 * cv.height) / (cv.width || 1)];
  if (seg.etapa === "recortes") {
    const laco = lacoSelecionado(anot);
    const v = laco ? verticeEm(p, laco, cv) : -1;
    if (v >= 0) {
      guardar(anot);
      // Cópia no lugar do original: o Ctrl+Z guarda os objetos, e mexer nos
      // vértices do próprio objeto guardado tornaria o arrasto indesfazível.
      const copia = { ...laco, pts: laco.pts.map((q) => [...q]) };
      anot.boxes[anot.sel] = copia;
      if (ev.button === 2 || ev.altKey) {     // apaga o vértice
        // a aresta nova pode atravessar uma reentrância da principal
        if (copia.pts.length > 3) { copia.pts.splice(v, 1); recortarLaco(anot, copia); }
        aoMudar(); return;
      }
      seg.arrastoLaco = { peca: copia, vertice: v };
      return;
    }
    const alvo = alcaDeLinhaEm(p, cv);
    if (alvo) {
      guardar(anot);
      const c = seg.cortes.get(seg.ativa);
      if (ev.button === 2 || ev.altKey) {     // apaga a linha inteira
        c.linhas.splice(alvo.linha, 1);
        refazer(seg.ativa, anot, fatias(seg.ativa, anot));
        aoMudar(); return;
      }
      seg.arrastoLinha = { ...alvo, referencia: fatias(seg.ativa, anot) };
      return;
    }
    // o gesto só se decide ao soltar: curto é clique, longo é traço
    if (ev.button === 0) seg.traco = { px: [[p.x, p.y]] };
    return;
  }
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
  const ed = seg.editando;
  if (ed) {
    const v = verticeEm(p, ed, cv);
    if (v >= 0) {
      if (ev.button === 2 || ev.altKey) {     // apagar o vértice
        if (ed.pts.length > 3) { ed.pts.splice(v, 1); anot.sujo = true; }
        aoMudar(); return;
      }
      seg.arrasto = { vertice: v }; return;
    }
    if (ev.button === 2) return;
    // Perto da aresta, pelo lado de fora: é o primeiro clique de um duplo
    // clique que vai inserir vértice, não o começo de outro contorno.
    if (arestaEm(p, ed, cv)) return;
    if (dentroDe(x, y, ed.pts) && !ev.shiftKey) {
      anot.sel = anot.boxes.indexOf(ed);
      // `px0`: o polígono só anda depois de 3 px de arrasto. Sem isso o
      // tremor entre os dois cliques de um duplo clique na aresta o movia.
      seg.arrasto = { mover: [x, y], px0: [p.x, p.y], solto: false };
      aoMudar(); return;
    }
    // clique fora da que está em edição: ela trava e começa a próxima
    if (!travar(anot)) return;
  }
  if (ev.button === 2) return;
  // Começa um contorno novo, mesmo em cima de uma principal travada: é o
  // que deixa marcar a segunda colada (ou dentro) da primeira.
  anot.sel = -1;
  seg.rascunho = usaSam()
    ? { sam: true, pontos: [{ x, y, positivo: true }], pts: null }
    : { sam: false, pts: [[x, y]] };
  aoMudar();
  if (seg.rascunho.sam) segmentar(aoMudar);
}

export function mover(p, { anot, cv, aoMudar }) {
  const lc = seg.arrastoLaco;
  if (lc) {
    lc.peca.pts[lc.vertice] = [clamp01(p.x / cv.width), clamp01(p.y / cv.height)];
    anot.sujo = true; aoMudar();
    return true;
  }
  const al = seg.arrastoLinha;
  if (al) {
    const c = seg.cortes.get(seg.ativa);
    if (!c) { seg.arrastoLinha = null; return true; }
    c.linhas[al.linha][al.vertice] = [p.x / cv.width, p.y / cv.height];
    refazer(seg.ativa, anot, al.referencia);
    aoMudar();
    return true;
  }
  const t = seg.traco;
  if (t) {
    const [ux, uy] = t.px[t.px.length - 1];
    if (Math.hypot(p.x - ux, p.y - uy) >= 3) { t.px.push([p.x, p.y]); aoMudar(); }
    return true;
  }
  const a = seg.arrasto, b = seg.editando;
  if (!a || !b) return false;
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

/** Fim do gesto. Sem `p` (ponteiro cancelado) o traço se descarta. */
export function soltar(p, ctx) {
  const lc = seg.arrastoLaco;
  // o laço continua dentro da principal: o vértice puxado para fora é
  // cortado pela borda dela, como no desenho
  if (lc && ctx) { recortarLaco(ctx.anot, lc.peca); ctx.aoMudar(); }
  seg.arrasto = null; seg.arrastoLinha = null; seg.arrastoLaco = null;
  const t = seg.traco;
  seg.traco = null;
  if (!t || !p) return;
  t.px.push([p.x, p.y]);
  let comprimento = 0;
  for (let i = 1; i < t.px.length; i++)
    comprimento += Math.hypot(t.px[i][0] - t.px[i - 1][0], t.px[i][1] - t.px[i - 1][1]);
  if (comprimento < 6) escolher(t.px[0], ctx);
  else if (seg.ferramenta === "fatiar") cortar(t.px, ctx);
  else lacar(t.px, ctx);
  ctx.aoMudar();
}

/** Traço em px → vértices normalizados, sem o tremor da mão. Não prende à
 *  imagem: a linha que sai da borda corta igual, e o laço é recortado. */
const doTraco = (px, cv, anel) => (anel ? simplificar(px, 1.5) : linha(px, 1.5))
  .map(([x, y]) => [x / cv.width, y / cv.height]);

/** Clique nos recortes: escolhe o pedaço (o menor sob o ponto, que é o de
 *  cima), ou passa a recortar a outra principal clicada. */
function escolher([px, py], { anot, cv }) {
  // Perto da aresta do laço selecionado é o primeiro clique de um duplo
  // clique que vai inserir vértice: trocar a seleção aqui o desfaria.
  const laco = lacoSelecionado(anot);
  if (laco && arestaEm({ x: px, y: py }, laco, cv)) return;
  const x = px / cv.width, y = py / cv.height;
  const a = seg.ativa;
  if (a && dentroDe(x, y, a.pts)) {
    let melhor = -1, menor = Infinity;
    anot.boxes.forEach((b, i) => {
      if (b.dono !== a || !dentroDe(x, y, b.pts)) return;
      const s = area(b.pts);
      if (s < menor) { menor = s; melhor = i; }
    });
    anot.sel = melhor;
    if (melhor >= 0 && anot.boxes[melhor].cls !== null) anot.classe = anot.boxes[melhor].cls;
    return;
  }
  const outra = principais(anot).find((p) => p !== a && dentroDe(x, y, p.pts) &&
                                              filhasDe(p.cls, anot).length);
  if (outra) {
    seg.ativa = outra;
    anot.classe = filhasDe(outra.cls, anot)[0];
  }
  anot.sel = -1;
}

// Menos que isso dentro de um pedaço é o começo do traço encostando na fatia
// vizinha, não a intenção de cortá-la. Medido numa imagem de 1000 de largura
// (≈ 11 px na tela sem zoom), e não em px da tela: as linhas são refeitas a
// cada arrasto, e com o limiar no zoom do momento uma linha antiga deixava
// de valer ao afastar a imagem — e a principal ficava sem fatia nenhuma.
const opcoesDoCorte = () => ({ escala: seg.escala, minimo: 25 });

/** Os pedaços de fatia da principal — o laço fica fora: é defeito marcado
 *  por cima, e o corte que passa por ele não o divide. */
const fatias = (p, anot) => anot.boxes.filter((b) => b.dono === p && !b.laco);

function cortar(px, { anot, cv }) {
  const a = seg.ativa;
  const traco = doTraco(px, cv);
  const r = fatiar(seg.pc, fatias(a, anot), a.pts, traco, opcoesDoCorte());
  if (!r.divididos) { avisar("a linha não atravessou o contorno"); return; }
  guardar(anot);
  if (!seg.cortes.has(a)) seg.cortes.set(a, { base: fatias(a, anot), linhas: [] });
  seg.cortes.get(a).linhas.push(traco);
  for (const p of r.pecas) p.dono = a;
  anot.boxes = [...anot.boxes.filter((b) => b.dono !== a || b.laco), ...r.pecas];
  anot.sel = -1; anot.sujo = true;
}

/** Refaz as fatias da principal a partir de `base` + linhas. A classe de
 *  cada pedaço novo vem de quem mais se sobrepõe a ele em `referencia` (os
 *  pedaços de antes do arrasto): o ponto anda, a classe fica no lugar. */
function refazer(p, anot, referencia) {
  const c = seg.cortes.get(p);
  let pecas = c.base.map((b) => ({ cls: b.cls, pts: b.pts, dono: p }));
  for (const l of c.linhas) pecas = fatiar(seg.pc, pecas, p.pts, l, opcoesDoCorte()).pecas;
  for (const n of pecas) {
    const cls = classePorSobreposicao(seg.pc, n, referencia);
    if (cls !== undefined) n.cls = cls;
    n.dono = p;
  }
  anot.boxes = [...anot.boxes.filter((b) => b.dono !== p || b.laco), ...pecas];
  anot.sel = -1; anot.sujo = true;
}

/** Alça de linha viva da principal ativa sob o ponto (px). */
function alcaDeLinhaEm(p, cv) {
  const c = seg.ativa && seg.cortes.get(seg.ativa);
  if (!c) return null;
  for (let l = c.linhas.length - 1; l >= 0; l--) {
    const v = c.linhas[l].findIndex((q) => {
      const [x, y] = emPx(q, cv);
      return Math.hypot(p.x - x, p.y - y) <= ALCA + 3;
    });
    if (v >= 0) return { linha: l, vertice: v };
  }
  return null;
}

function lacar(px, { anot, cv }) {
  const a = seg.ativa;
  // anel: o laço costuma terminar onde começou, e a linha aberta com as
  // pontas iguais perderia todos os vértices (ver `simplificar`)
  const pts = doTraco(px, cv, true);
  const aneis = pts.length >= 3 ? recortarPor(seg.pc, pts, a.pts) : [];
  if (!aneis.length) { avisar("o laço ficou fora do contorno"); return; }
  guardar(anot);
  const irmas = filhasDe(a.cls, anot);
  const cls = irmas.includes(anot.classe) ? anot.classe : irmas[0];
  for (const pts of aneis) anot.boxes.push({ cls, pts, dono: a, laco: true });
  // selecionado: o número (ou a lista) já troca a classe dele
  anot.sel = anot.boxes.length - 1; anot.sujo = true;
}

/** Cursor conforme o que está sob o ponteiro. */
export function cursor(p, { anot, cv }) {
  if (seg.etapa === "recortes") {
    const laco = lacoSelecionado(anot);
    if ((laco && verticeEm(p, laco, cv) >= 0) || alcaDeLinhaEm(p, cv)) return "grab";
  }
  const ed = seg.editando;
  if (seg.etapa === "contorno" && !seg.rascunho && ed && verticeEm(p, ed, cv) >= 0)
    return "grab";
  return "crosshair";
}

/** Recorta o laço `peca` pela borda da principal dele, no lugar. A borda
 *  côncava pode partir o laço em dois: os dois ficam (guardar só o maior
 *  apagava o outro sem aviso). Todo fora, o laço some. */
function recortarLaco(anot, peca) {
  const i = anot.boxes.indexOf(peca);
  if (i < 0 || !peca.dono || !seg.pc) return;
  const aneis = recortarPor(seg.pc, peca.pts, peca.dono.pts);
  anot.boxes.splice(i, 1, ...aneis.map((pts) => ({ ...peca, pts })));
  anot.sel = aneis.length ? i : -1;
  anot.sujo = true;
}

/** O laço selecionado na principal ativa: o que ganha alças nos recortes. */
function lacoSelecionado(anot) {
  const b = anot.boxes[anot.sel];
  return seg.etapa === "recortes" && b && b.laco && b.dono === seg.ativa ? b : null;
}

/** Duplo clique numa aresta insere um vértice ali: na principal em edição,
 *  ou no laço selecionado. */
export function duploClique(p, { anot, cv, aoMudar }) {
  const laco = lacoSelecionado(anot);
  if (laco) {
    const a = arestaEm(p, laco, cv);
    if (!a) return;
    guardar(anot);
    const copia = { ...laco, pts: laco.pts.map((q) => [...q]) };
    copia.pts.splice(a.indice, 0, a.pt);
    anot.boxes[anot.sel] = copia;
    anot.sujo = true; aoMudar();
    return;
  }
  const ed = seg.editando;
  if (seg.etapa !== "contorno" || seg.rascunho || !ed) return;
  const a = arestaEm(p, ed, cv);
  if (!a) return;
  ed.pts.splice(a.indice, 0, a.pt);
  anot.sujo = true; aoMudar();
}

/** O contorno do rascunho vira principal, na classe atual, e fica em edição
 *  até o próximo Enter (ou o próximo clique fora dela). */
export function aceitar(anot, aoMudar) {
  const r = seg.rascunho;
  const pts = r && r.pts;
  if (r && r.sam && !contornoEmDia()) return "o SAM ainda está calculando — um instante";
  if (!pts || pts.length < 3 || area(pts) < 1e-6) {
    return "o contorno ainda não tem área — clique mais pontos";
  }
  const b = { cls: anot.classe,
              pts: pts.map(([x, y]) => [+x.toFixed(6), +y.toFixed(6)]) };
  anot.boxes.push(b);
  abrirEdicao(b);
  anot.sel = anot.boxes.length - 1;
  anot.sujo = true;
  seg.rascunho = null;
  aoMudar();
  return null;
}

/** Teclas próprias da segmentação. Devolve true se consumiu a tecla. */
export function tecla(ev, anot, aoMudar, avisar) {
  const k = ev.key.toLowerCase();
  if ((ev.ctrlKey || ev.metaKey) && k === "z") {
    ev.preventDefault();
    if (seg.etapa === "recortes" && desfazer(anot)) aoMudar();
    return true;
  }
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return false;
  if (ev.key === "Enter" && seg.rascunho) {
    ev.preventDefault();
    const erro = aceitar(anot, aoMudar);
    if (erro) avisar(erro, true);
    return true;
  }
  if (ev.key === "Enter" && seg.editando) {
    ev.preventDefault(); travar(anot); aoMudar(); return true;
  }
  if (ev.key === "Escape") {
    if (seg.rascunho) { seg.rascunho = null; aoMudar(); return true; }
    if (seg.traco) { seg.traco = null; aoMudar(); return true; }
    if (seg.etapa === "recortes") { mudarEtapa("contorno", anot); aoMudar(); return true; }
    return false;
  }
  if (ev.key === "Backspace" && seg.rascunho) {
    ev.preventDefault();
    const r = seg.rascunho;
    if (r.sam) { r.pontos.pop(); if (!r.pontos.length) seg.rascunho = null;
                 else segmentar(aoMudar); }
    else { r.pts.pop(); if (!r.pts.length) seg.rascunho = null; }
    aoMudar(); return true;
  }
  if (k === "m" && seg.etapa === "contorno") {
    seg.manual = !seg.manual; seg.rascunho = null; aoMudar(); return true;
  }
  if ((k === "t" || k === "x") && seg.etapa === "recortes") {
    const erro = k === "t" ? aplicarGabarito(anot) : espelhar(anot);
    if (erro) avisar(erro, true);
    aoMudar(); return true;
  }
  const etapa = { c: "contorno", r: "recortes" }[k];
  const ferramenta = { f: "fatiar", g: "desenhar" }[k];
  if (etapa || ferramenta) {
    const erro = mudarEtapa(etapa || "recortes", anot);
    if (erro) avisar(erro, true);
    else if (ferramenta) mudarFerramenta(ferramenta, anot);
    aoMudar(); return true;
  }
  return false;
}

/** O que a ajuda (?) diz agora: o passo a passo da etapa. */
export function ajuda() {
  if (seg.etapa === "recortes") {
    return (seg.ferramenta === "fatiar"
      ? "Fatiar: arraste uma linha atravessando o contorno — ela divide o " +
        "pedaço (ou a principal inteira) em dois. Repita para cada corte."
      : "Desenhar: arraste em volta da região. O que passar da borda da " +
        "principal é cortado.") +
      "\nClique num pedaço e escolha a classe (número ou lista). Pedaço " +
      "tracejado ainda não tem classe.\nAs linhas dos cortes ficam com " +
      "alças: arraste para ajustar, botão direito apaga a linha.\n" +
      "Laço selecionado: arraste os vértices · Alt+clique apaga · duplo " +
      "clique na aresta insere.\n" +
      "t aplica o gabarito · x espelha · \"Salvar gabarito\" guarda os " +
      "recortes desta como modelo.\nCtrl+Z desfaz · d apaga o pedaço · " +
      "clique noutra principal para recortá-la · Esc volta ao contorno.";
  }
  if (seg.editando) {
    return "Ajuste: arraste os vértices · Alt+clique (ou botão direito) " +
           "apaga · duplo clique na aresta insere.\nEnter (ou clicar fora) " +
           "trava este contorno e libera o próximo.";
  }
  return (seg.manual
    ? "À mão: clique nos vértices; clique no primeiro (ou Enter) fecha."
    : "Clique no objeto: o SAM contorna. Clique de novo para incluir, botão " +
      "direito (ou Shift) para tirar. Enter aceita · Esc descarta · " +
      "m desenha à mão.") +
    "\nO contorno aceito trava — clique no próximo objeto. Para mexer num " +
    "travado: \"editar\" na lista. Depois, r divide em recortes.";
}

// ---------------------------------------------------------- desenho --

const SEM_CLASSE = "#c8ccd0";
const corDe = (cls) => (cls === null ? SEM_CLASSE : cor(cls));

export function desenhar(cx, cv, anot) {
  const rec = seg.etapa === "recortes";
  const sel = anot.boxes[anot.sel];
  const ps = principais(anot), filhos = anot.boxes.filter((b) => !ehPrincipal(b, anot));

  // principais embaixo, só o preenchimento: com recortes por cima, a cor
  // forte fica com os pedaços
  for (const p of ps) {
    const temFilhos = filhos.some((b) => b.dono === p);
    const alfa = p === seg.editando ? "55"
      : rec ? (p === seg.ativa && !temFilhos ? "22" : "0a")
      : temFilhos ? "14" : "33";
    caminho(cx, p.pts, cv);
    cx.fillStyle = cor(p.cls) + alfa; cx.fill();
  }
  for (const b of filhos) {
    const apagado = rec && b.dono !== seg.ativa;
    caminho(cx, b.pts, cv);
    cx.fillStyle = corDe(b.cls) + (b.cls === null ? "1c" : apagado ? "18" : b === sel ? "88" : "55");
    cx.fill();
    if (b.cls === null) cx.setLineDash([5, 4]);
    cx.lineWidth = b === sel ? 3 : 1.5;
    cx.strokeStyle = b === sel ? "#ffffff" : corDe(b.cls) + (apagado ? "55" : "");
    cx.stroke(); cx.setLineDash([]);
  }
  // contornos por cima de tudo: a borda que os recortes herdam
  for (const p of ps) {
    caminho(cx, p.pts, cv);
    const forte = p === seg.editando || p === seg.ativa || (!rec && p === sel);
    cx.lineWidth = forte ? 3 : 2;
    cx.strokeStyle = cor(p.cls) + (rec && p !== seg.ativa ? "66" : "");
    cx.stroke();
  }
  if (anot.nomes) {
    for (const p of ps) rotulo(cx, p, anot, emPx(p.pts.reduce((a, v) => (v[1] < a[1] ? v : a)), cv));
    for (const b of filhos) {
      if (rec && b.dono !== seg.ativa) continue;
      const n = b.pts.length;
      rotulo(cx, b, anot, emPx([b.pts.reduce((s, v) => s + v[0], 0) / n,
                                    b.pts.reduce((s, v) => s + v[1], 0) / n], cv));
    }
  }
  if (seg.editando) for (const v of seg.editando.pts) alca(cx, emPx(v, cv), cor(seg.editando.cls));
  const lacoSel = lacoSelecionado(anot);
  if (lacoSel) for (const v of lacoSel.pts) alca(cx, emPx(v, cv), corDe(lacoSel.cls));
  const vivos = rec && seg.ativa && seg.cortes.get(seg.ativa);
  if (vivos) {
    for (const l of vivos.linhas) {
      cx.beginPath();
      l.forEach((v, i) => { const [x, y] = emPx(v, cv); if (i) cx.lineTo(x, y); else cx.moveTo(x, y); });
      cx.lineWidth = 1.5; cx.strokeStyle = "#ffd60aaa"; cx.stroke();
      for (const v of l) {
        const [x, y] = emPx(v, cv);
        cx.beginPath(); cx.arc(x, y, 4, 0, 2 * Math.PI);
        cx.fillStyle = "#0d1217"; cx.fill();
        cx.lineWidth = 2; cx.strokeStyle = "#ffd60a"; cx.stroke();
      }
    }
  }

  const t = seg.traco;
  if (t && t.px.length > 1) {
    cx.beginPath();
    t.px.forEach(([x, y], i) => (i ? cx.lineTo(x, y) : cx.moveTo(x, y)));
    if (seg.ferramenta === "desenhar") {
      cx.closePath(); cx.fillStyle = cor(anot.classe) + "33"; cx.fill();
    }
    cx.setLineDash([6, 4]); cx.lineWidth = 2;
    cx.strokeStyle = seg.ferramenta === "fatiar" ? "#ffd60a" : cor(anot.classe);
    cx.stroke(); cx.setLineDash([]);
  }

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

function rotulo(cx, b, anot, [x, y]) {
  const nome = b.cls === null ? "?" : (anot.classes[b.cls] || {}).nome || String(b.cls);
  cx.font = "600 11px ui-monospace, monospace";
  const larg = cx.measureText(nome).width + 8;
  const acima = y >= 15;
  cx.fillStyle = corDe(b.cls);
  cx.fillRect(x, acima ? y - 15 : y, larg, 15);
  cx.fillStyle = "#0d1217";
  cx.fillText(nome, x + 4, acima ? y - 4 : y + 11);
}
