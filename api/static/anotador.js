/* O anotador: canvas, alças, teclado e a gravação da caixa. Em projeto de
 * segmentação, o desenho e a edição são do `segmentador.js` (polígono com
 * SAM); o resto — navegação, classes, gravação — é o mesmo. */

import { mostrarAba } from "./abas.js";
import { $, api, avisar, clamp, clamp01, cor, erroAviso, esc, estado, post } from "./nucleo.js";
import { hachurarFora } from "./roi.js";
import { carregarDados, dados, desenharGrade, preencherEscopo } from "./rotulagem.js";
import * as segm from "./segmentador.js";

const segmentacao = () => estado.tarefa === "segment";
const ctxSeg = () => ({ anot, cv: $("anotCanvas"), aoMudar: redesenhar });

const anot = { id: null, boxes: [], classe: 0, classes: [], sel: -1,
               desenhando: null, img: null, indice: -1, sujo: false,
               // preferência lembrada: quem rotula o dia todo não quer
               // remarcar a caixinha a cada sessão
               nomes: localStorage.getItem("visionyard.nomes") !== "0",
               arrasto: null, fit: 1, zoom: 1, roi: null,
               // cresce a cada abertura: resposta de uma abertura anterior
               // (clique rápido na grade, q/e seguidos) não pode vencer a atual
               geracao: 0 };

export async function abrirAnotador(id) {
  const geracao = ++anot.geracao;
  segm.esquecer();
  $("anotSam").textContent = "";   // o rótulo da imagem anterior não vale aqui
  const d = await api(`/projects/${estado.projeto}/samples/${id}`);
  if (geracao !== anot.geracao) return;
  anot.id = id;
  anot.classes = (estado.classes || []).map((c, i) => ({ nome: c, cor: cor(i) }));
  anot.boxes = (d.minha_anotacao && d.minha_anotacao.boxes) || [];
  anot.sel = -1;
  anot.classe = 0;
  anot.sujo = false;
  anot.indice = dados.itens.findIndex((x) => x.id === id);
  $("anotNomes").checked = anot.nomes;

  const dsAtual = estado.datasets.find((x) => x.slug === dados.dataset);
  // A ROI só existe quando um dataset está no escopo da rotulagem. O overlay é
  // informativo: mostra o que o treino vai recortar; não trava o desenho.
  anot.roi = (dsAtual && dsAtual.roi) || null;
  const badge = $("anotDataset");
  if (dsAtual) { badge.textContent = dsAtual.name; badge.hidden = false; }
  else { badge.hidden = true; }
  $("anotArquivo").textContent = d.uri.split("/").pop();
  $("anotOutros").textContent = d.outros_anotaram
    ? `${d.outros_anotaram} outro(s) rotulador(es) já anotaram esta imagem. ` +
      `As caixas deles não aparecem: ver antes de desenhar enviesa a sua.`
    : "";
  $("anotDica").innerHTML =
    `<span class="anot-progresso">${anot.indice + 1}/${dados.itens.length}` +
    ` · pág ${dados.pagina + 1} · ${segmentacao()
      ? "clique no objeto · Enter aceita · Esc descarta"
      : "arraste as alças p/ ajustar"}</span>`;
  $("anotRotuloLista").textContent = segmentacao() ? "Contornos" : "Caixas";
  $("anotador").hidden = false;

  const img = $("anotImagem");
  anot.img = img;
  await new Promise((ok, falha) => {
    img.onload = ok;
    img.onerror = () => falha(new Error("não consegui carregar a imagem"));
    img.src = d.url || "";
    if (!d.url) falha(new Error("imagem sem URL assinada"));
  }).catch((e) => avisar(e.message, true));
  if (geracao !== anot.geracao) return;

  // Cada imagem abre inteira (fit) e sem zoom; a roda amplia a partir daí.
  anot.zoom = 1;
  calcularFit();
  renderImagem();
  $("anotTela").scrollTo(0, 0);
  desenharClasses();
  // Sem await: a imagem já está na tela e dá para começar à mão enquanto o
  // encoder roda. Contorno começado à mão continua à mão; os que começarem
  // depois de "SAM pronto" usam o SAM.
  if (segmentacao()) segm.prepararImagem(d.url_sam, redesenhar);
}

/** Escala que faz a imagem inteira caber na área, sem cortar. */
function calcularFit() {
  const img = $("anotImagem");
  const tela = $("anotTela").getBoundingClientRect();
  if (!img.naturalWidth || !tela.width) { anot.fit = 1; return; }
  anot.fit = Math.min(tela.width / img.naturalWidth,
                      tela.height / img.naturalHeight);
}

/** Aplica fit × zoom: dimensiona imagem, wrapper e canvas juntos. O canvas
 *  fica em resolução 1:1 com o tamanho exibido, então o mapeamento de
 *  ponteiro (posCanvas) não muda — só a escala. */
function renderImagem() {
  const img = $("anotImagem"), cv = $("anotCanvas"), zoom = $("anotZoom");
  if (!img.naturalWidth) return;
  const w = Math.round(img.naturalWidth * anot.fit * anot.zoom);
  const h = Math.round(img.naturalHeight * anot.fit * anot.zoom);
  img.style.width = zoom.style.width = w + "px";
  img.style.height = zoom.style.height = h + "px";
  cv.width = w; cv.height = h;
  cv.style.width = w + "px"; cv.style.height = h + "px";
  redesenhar();
}

window.addEventListener("resize", () => {
  if ($("anotador").hidden) return;
  calcularFit();
  anot.zoom = Math.max(1, anot.zoom);
  renderImagem();
});

// Roda do mouse dá zoom, mantendo sob o cursor o mesmo ponto da imagem.
$("anotTela").addEventListener("wheel", (ev) => {
  if ($("anotador").hidden) return;
  ev.preventDefault();
  const tela = $("anotTela");
  const antes = anot.zoom;
  const fator = ev.deltaY < 0 ? 1.15 : 1 / 1.15;
  anot.zoom = clamp(anot.zoom * fator, 1, 12);
  if (anot.zoom === antes) return;

  // ponto da imagem sob o cursor, antes de reescalar
  const cv = $("anotCanvas"), r = cv.getBoundingClientRect();
  const fx = (ev.clientX - r.left) / r.width;
  const fy = (ev.clientY - r.top) / r.height;
  renderImagem();
  // reposiciona o scroll para o mesmo ponto ficar sob o cursor
  const telaR = tela.getBoundingClientRect();
  tela.scrollLeft = fx * cv.width - (ev.clientX - telaR.left);
  tela.scrollTop = fy * cv.height - (ev.clientY - telaR.top);
}, { passive: false });

function desenharClasses() {
  $("anotClasses").innerHTML = anot.classes.map((c, i) => `
    <li><button data-classe="${i}" aria-current="${i === anot.classe}">
      <span class="cor" style="background:${c.cor}"></span>
      ${esc(c.nome)}<span class="tecla">${i + 1}</span></button></li>`).join("");
  $("anotClasses").querySelectorAll("[data-classe]").forEach((b) =>
    b.addEventListener("click", () => {
      anot.classe = Number(b.dataset.classe);
      // trocar a classe com uma caixa selecionada reclassifica ela
      if (anot.sel >= 0) { anot.boxes[anot.sel].cls = anot.classe;
                           anot.sujo = true; redesenhar(); }
      desenharClasses();
    }));
}

function listarCaixas() {
  $("anotCaixas").innerHTML = anot.boxes.length
    ? anot.boxes.map((b, i) => `
        <li class="${i === anot.sel ? "selecionada" : ""}" data-caixa="${i}">
          <span class="cor" style="background:${cor(b.cls)}"></span>
          ${esc((anot.classes[b.cls] || {}).nome || b.cls)}
          <button class="discreto" data-apagar="${i}">apagar</button></li>`).join("")
    : `<li class="vazio">nenhum${segmentacao() ? " contorno" : "a caixa"} —
         salvar assim marca a imagem como "olhei, não há nada"</li>`;
  $("anotCaixas").querySelectorAll("[data-caixa]").forEach((li) =>
    li.addEventListener("click", (ev) => {
      if (ev.target.dataset.apagar !== undefined) return;
      anot.sel = Number(li.dataset.caixa); redesenhar();
    }));
  $("anotCaixas").querySelectorAll("[data-apagar]").forEach((b) =>
    b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      anot.boxes.splice(Number(b.dataset.apagar), 1);
      anot.sel = -1; anot.sujo = true; redesenhar();
    }));
}

function redesenhar() {
  const cv = $("anotCanvas"), cx = cv.getContext("2d");
  cx.clearRect(0, 0, cv.width, cv.height);
  // Recorte do dataset em escopo: escurece e hachura o que o treino vai jogar
  // fora, e contorna a ROI. Fica sob as caixas, que seguem visíveis por cima.
  if (anot.roi) {
    const r = anot.roi;
    const rx = r.x * cv.width, ry = r.y * cv.height,
          rw = r.w * cv.width, rh = r.h * cv.height;
    hachurarFora(cx, cv.width, cv.height, rx, ry, rw, rh);
    cx.strokeStyle = "#ffd60a";
    cx.lineWidth = 2;
    cx.setLineDash([6, 4]);
    cx.strokeRect(rx, ry, rw, rh);
    cx.setLineDash([]);
  }
  $("anotSam").textContent = segmentacao() ? segm.situacaoSam() : "";
  $("anotSam").title = segm.seg.situacao === "indisponivel" ? segm.seg.motivo : "";
  if (segmentacao()) {
    segm.desenhar(cx, cv, anot);
    listarCaixas();
    return;
  }
  anot.boxes.forEach((b, i) => {
    const x = (b.cx - b.w / 2) * cv.width, y = (b.cy - b.h / 2) * cv.height;
    const w = b.w * cv.width, h = b.h * cv.height;
    cx.lineWidth = i === anot.sel ? 3 : 2;
    cx.strokeStyle = cor(b.cls);
    cx.strokeRect(x, y, w, h);
    if (i === anot.sel) {
      cx.fillStyle = cor(b.cls) + "22";
      cx.fillRect(x, y, w, h);
    }
    if (!anot.nomes) return;
    const nome = (anot.classes[b.cls] || {}).nome || String(b.cls);
    cx.font = "600 11px ui-monospace, monospace";
    const larg = cx.measureText(nome).width + 8;
    // acima da caixa; se não couber (caixa colada no topo), vai para dentro
    const acima = y >= 15;
    cx.fillStyle = cor(b.cls);
    cx.fillRect(x, acima ? y - 15 : y, larg, 15);
    cx.fillStyle = "#0d1217";
    cx.fillText(nome, x + 4, (acima ? y - 4 : y + 11));
  });
  // Alças de resize na caixa selecionada — só nela, para não poluir a tela.
  if (anot.sel >= 0) {
    const b = anot.boxes[anot.sel];
    for (const [hx, hy] of pontosAlca(b, cv)) {
      cx.fillStyle = "#0d1217";
      cx.strokeStyle = cor(b.cls);
      cx.lineWidth = 2;
      cx.fillRect(hx - ALCA, hy - ALCA, ALCA * 2, ALCA * 2);
      cx.strokeRect(hx - ALCA, hy - ALCA, ALCA * 2, ALCA * 2);
    }
  }
  if (anot.desenhando) {
    const d = anot.desenhando;
    cx.setLineDash([4, 3]);
    cx.strokeStyle = cor(anot.classe);
    cx.lineWidth = 2;
    cx.strokeRect(d.x0, d.y0, d.x1 - d.x0, d.y1 - d.y0);
    cx.setLineDash([]);
  }
  listarCaixas();
}

const ALCA = 5;  // meio-lado da alça, em px

// As 8 alças em px, junto com o código de qual borda cada uma move.
// n/s/e/w = norte/sul/leste/oeste; as diagonais combinam dois.
function pontosAlca(b, cv) {
  const x1 = (b.cx - b.w / 2) * cv.width, y1 = (b.cy - b.h / 2) * cv.height;
  const x2 = (b.cx + b.w / 2) * cv.width, y2 = (b.cy + b.h / 2) * cv.height;
  const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
  return [[x1, y1, "nw"], [mx, y1, "n"], [x2, y1, "ne"],
          [x2, my, "e"], [x2, y2, "se"], [mx, y2, "s"],
          [x1, y2, "sw"], [x1, my, "w"]];
}

const CURSOR = {nw: "nwse-resize", se: "nwse-resize", ne: "nesw-resize",
                sw: "nesw-resize", n: "ns-resize", s: "ns-resize",
                e: "ew-resize", w: "ew-resize", mover: "move"};

const posCanvas = (ev) => {
  const r = $("anotCanvas").getBoundingClientRect();
  return { x: ev.clientX - r.left, y: ev.clientY - r.top };
};

/** O que está sob o ponto: uma alça da caixa selecionada, o corpo de alguma
 *  caixa (para mover), ou nada (para desenhar). */
function alvoEm(p, cv) {
  if (anot.sel >= 0) {
    for (const [hx, hy, borda] of pontosAlca(anot.boxes[anot.sel], cv)) {
      if (Math.abs(p.x - hx) <= ALCA + 2 && Math.abs(p.y - hy) <= ALCA + 2)
        return { tipo: "resize", borda };
    }
  }
  // topo primeiro: a caixa desenhada por último fica por cima
  for (let i = anot.boxes.length - 1; i >= 0; i--) {
    const b = anot.boxes[i];
    if (Math.abs(p.x / cv.width - b.cx) < b.w / 2 &&
        Math.abs(p.y / cv.height - b.cy) < b.h / 2)
      return { tipo: "mover", indice: i };
  }
  return { tipo: "desenhar" };
}

$("anotCanvas").addEventListener("pointerdown", (ev) => {
  const cv = $("anotCanvas"), p = posCanvas(ev);
  if (segmentacao()) {
    ev.target.setPointerCapture(ev.pointerId);
    segm.apertar(ev, p, ctxSeg());
    return;
  }
  const alvo = alvoEm(p, cv);

  if (alvo.tipo === "resize") {
    anot.arrasto = { modo: "resize", borda: alvo.borda };
  } else if (alvo.tipo === "mover" && !ev.shiftKey) {
    anot.sel = alvo.indice;
    const b = anot.boxes[anot.sel];
    // guarda o deslocamento do ponto ao centro, para mover sem "pular"
    anot.arrasto = { modo: "mover",
                     dx: p.x / cv.width - b.cx, dy: p.y / cv.height - b.cy };
  } else {
    anot.desenhando = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
  }
  ev.target.setPointerCapture(ev.pointerId);
  redesenhar();
});

$("anotCanvas").addEventListener("pointermove", (ev) => {
  const cv = $("anotCanvas"), p = posCanvas(ev);
  if (segmentacao()) {
    if (!segm.mover(p, ctxSeg())) cv.style.cursor = segm.cursor(p, ctxSeg());
    return;
  }
  const fx = clamp01(p.x / cv.width), fy = clamp01(p.y / cv.height);

  if (anot.desenhando) {
    // Prende o desenho às bordas da imagem: arrastar para fora não estica a
    // caixa além dela. (Move e resize já são presos abaixo.)
    anot.desenhando.x1 = clamp(p.x, 0, cv.width);
    anot.desenhando.y1 = clamp(p.y, 0, cv.height);
    redesenhar();
  } else if (anot.arrasto && anot.arrasto.modo === "mover") {
    const b = anot.boxes[anot.sel];
    // mantém a caixa inteira dentro da imagem
    b.cx = clamp(fx - anot.arrasto.dx, b.w / 2, 1 - b.w / 2);
    b.cy = clamp(fy - anot.arrasto.dy, b.h / 2, 1 - b.h / 2);
    anot.sujo = true; redesenhar();
  } else if (anot.arrasto && anot.arrasto.modo === "resize") {
    redimensionar(anot.boxes[anot.sel], anot.arrasto.borda, fx, fy);
    anot.sujo = true; redesenhar();
  } else {
    // sem arrasto: só ajusta o cursor conforme o que está sob o ponteiro
    const alvo = alvoEm(p, cv);
    cv.style.cursor = alvo.tipo === "resize" ? CURSOR[alvo.borda]
      : alvo.tipo === "mover" ? CURSOR.mover : "crosshair";
  }
});

// Ponteiro cancelado (toque interrompido, captura perdida) não manda
// pointerup: sem isto o arrasto seguia preso ao próximo movimento.
$("anotCanvas").addEventListener("pointercancel", () => {
  segm.soltar(); anot.arrasto = null; anot.desenhando = null; redesenhar();
});

$("anotCanvas").addEventListener("pointerup", () => {
  if (segmentacao()) { segm.soltar(); return; }
  if (anot.arrasto) {
    // resize por um lado só pode ter cruzado o oposto: normaliza w/h > 0
    if (anot.sel >= 0) normalizarCaixa(anot.boxes[anot.sel]);
    anot.arrasto = null; redesenhar(); return;
  }
  const d = anot.desenhando;
  anot.desenhando = null;
  if (!d) return;
  const cv = $("anotCanvas");
  const x = Math.min(d.x0, d.x1), y = Math.min(d.y0, d.y1);
  const w = Math.abs(d.x1 - d.x0), h = Math.abs(d.y1 - d.y0);
  // arrasto minúsculo é clique numa área vazia: só limpa a seleção
  if (w < 6 || h < 6) { anot.sel = -1; redesenhar(); return; }
  anot.boxes.push({
    cls: anot.classe,
    cx: clamp01((x + w / 2) / cv.width), cy: clamp01((y + h / 2) / cv.height),
    w: Math.min(1, w / cv.width), h: Math.min(1, h / cv.height),
  });
  anot.sel = anot.boxes.length - 1;
  anot.sujo = true;
  redesenhar();
});

// O botão direito é ponto negativo do SAM e apaga vértice: o menu do
// navegador por cima atrapalharia os dois.
$("anotCanvas").addEventListener("contextmenu", (ev) => {
  if (segmentacao()) ev.preventDefault();
});
$("anotCanvas").addEventListener("dblclick", (ev) => {
  if (segmentacao()) segm.duploClique(posCanvas(ev), ctxSeg());
});

/** Move a(s) borda(s) indicada(s) para o ponto (fx,fy), em coordenadas 0..1.
 *  Trabalha em cantos e reconstrói centro/tamanho, para o arrasto ser
 *  intuitivo mesmo quando um lado ultrapassa o outro. */
function redimensionar(b, borda, fx, fy) {
  let e = b.cx - b.w / 2, o = b.cx + b.w / 2;   // esquerda, direita
  let n = b.cy - b.h / 2, s = b.cy + b.h / 2;   // topo, base
  if (borda.includes("w")) e = fx;
  if (borda.includes("e")) o = fx;
  if (borda.includes("n")) n = fy;
  if (borda.includes("s")) s = fy;
  b.cx = (e + o) / 2; b.cy = (n + s) / 2;
  b.w = Math.abs(o - e); b.h = Math.abs(s - n);
}

/** Depois de um resize que cruzou o lado oposto, garante w/h mínimos e a
 *  caixa dentro da imagem. */
function normalizarCaixa(b) {
  b.w = clamp(b.w, 0.004, 1); b.h = clamp(b.h, 0.004, 1);
  b.cx = clamp(b.cx, b.w / 2, 1 - b.w / 2);
  b.cy = clamp(b.cy, b.h / 2, 1 - b.h / 2);
}

document.addEventListener("keydown", (ev) => {
  if ($("anotador").hidden) return;
  // `matches` só existe em Element: quando o foco está no próprio documento
  // (nada focado, ou evento sintético), `ev.target` é o Document e a chamada
  // estoura — derrubando o atalho inteiro.
  if (ev.target instanceof Element &&
      ev.target.matches("input, textarea, select")) return;
  const k = ev.key.toLowerCase();

  // Antes do Escape geral: com um contorno em rascunho, Esc o descarta em
  // vez de fechar a imagem.
  if (segmentacao() && segm.tecla(ev, anot, redesenhar, avisar)) return;
  if (ev.key === "Escape") { fecharAnotador(); return; }

  if (k === "d" || ev.key === "Delete" || ev.key === "Backspace") {
    if (anot.sel < 0) return;
    ev.preventDefault();
    anot.boxes.splice(anot.sel, 1);
    anot.sel = -1; anot.sujo = true; redesenhar();
    return;
  }
  if (k === "q") { ev.preventDefault(); irParaImagem(-1).catch(erroAviso); return; }
  if (k === "e") { ev.preventDefault(); irParaImagem(+1).catch(erroAviso); return; }
  if (k === "w") { ev.preventDefault(); prerotular(); return; }

  const n = Number(ev.key);
  if (n >= 1 && n <= anot.classes.length) {
    anot.classe = n - 1;
    if (anot.sel >= 0) { anot.boxes[anot.sel].cls = anot.classe; anot.sujo = true; }
    desenharClasses(); redesenhar();
  }
});

/** Anterior/próxima dentro da página, buscando a página vizinha na borda.
 *
 * Salva antes de sair SÓ se algo mudou: navegar sem alterar nada não pode
 * gravar uma anotação vazia, que no modelo significa "olhei e não há nada".
 */
async function irParaImagem(passo) {
  if (segmentacao() && segm.seg.rascunho) {
    avisar("há um contorno em rascunho — Enter aceita, Esc descarta", true);
    return;
  }
  if (anot.sujo) await salvarAnotacao({ silencioso: true });

  let i = anot.indice + passo;
  if (i < 0 || i >= dados.itens.length) {
    const alvo = dados.pagina + (passo > 0 ? 1 : -1);
    if (alvo < 0) { avisar("primeira imagem"); return; }
    const antes = dados.itens.length;
    dados.pagina = alvo;
    await carregarDados();
    if (!dados.itens.length) {
      dados.pagina -= (passo > 0 ? 1 : -1);
      await carregarDados();
      avisar(passo > 0 ? "última imagem" : "primeira imagem");
      return;
    }
    i = passo > 0 ? 0 : dados.itens.length - 1;
    void antes;
  }
  await abrirAnotador(dados.itens[i].id);
}

/** Tecla w: roda o modelo NESTA imagem e traz as caixas para corrigir.
 *
 * É um pré-tag de escopo-de-imagem: enfileira, espera o agente prever, e carrega
 * o resultado no canvas. Só faz sentido numa imagem em branco — o modelo não
 * sobrescreve o que você já desenhou. */
async function prerotular() {
  if (anot.boxes.length || segm.seg.rascunho) {
    avisar("esta imagem já tem anotação — o pré-tag é para começar do zero");
    return;
  }
  // A espera é longa (segundos): a pessoa pode desenhar ou trocar de imagem
  // no meio. O resultado só entra se ainda for a mesma abertura e nada foi
  // desenhado; senão fica gravado e aparece ao reabrir.
  const geracao = anot.geracao, projeto = estado.projeto, sampleId = anot.id;
  const modelos = await api(`/projects/${estado.projeto}/models`).catch(() => []);
  if (!modelos.length) {
    avisar("nenhum modelo treinado ainda — treine um na aba Treinamento", true);
    return;
  }
  // prefere quem tem champion; senão o primeiro modelo
  const m = modelos.find((x) => x.aliases && x.aliases.champion) || modelos[0];
  const btn = $("anotPretag");
  const rotulo = btn.textContent;
  btn.disabled = true; btn.textContent = "…";
  try {
    const r = await post(`/projects/${projeto}/prelabel`, {
      model_slug: m.slug, sample_id: sampleId, conf: 0.25, only_missing: false,
    });
    avisar(`rodando ${m.name} v${r.model_version} nesta imagem…`);
    if (!await esperarPrelabelSample(r.job_id, projeto)) return;
    const d = await api(`/projects/${projeto}/samples/${sampleId}`);
    const boxes = (d.minha_anotacao && d.minha_anotacao.boxes) || [];
    if (!boxes.length) { avisar("o modelo não encontrou nada aqui"); return; }
    if (geracao !== anot.geracao || anot.boxes.length || segm.seg.rascunho) {
      avisar("o pré-tag terminou, mas você já mexeu aqui — ele não foi " +
             "aplicado por cima do seu desenho");
      return;
    }
    anot.boxes = boxes; anot.sel = -1; anot.sujo = false; redesenhar();
    avisar(`${boxes.length} ${segmentacao() ? "contorno(s)" : "caixa(s)"}` +
           " do modelo — corrija e salve");
  } catch (e) { avisar(e.message, true); }
  finally { btn.disabled = false; btn.textContent = rotulo; }
}

async function esperarPrelabelSample(jobId, projeto) {
  const url = `/projects/${projeto}/jobs/${jobId}`;
  for (let i = 0; i < 120; i++) {
    let j;
    try { j = await api(url); } catch { return false; }
    if (j.status === "succeeded") return true;
    if (j.status === "failed") { avisar(j.error || "pré-tag falhou", true); return false; }
    if (j.status === "canceled") return false;
    await new Promise((r) => setTimeout(r, 1200));
  }
  avisar("o pré-tag demorou demais — há uma máquina de inferência no ar?", true);
  return false;
}

$("anotNomes").addEventListener("change", (ev) => {
  anot.nomes = ev.target.checked;
  localStorage.setItem("visionyard.nomes", anot.nomes ? "1" : "0");
  redesenhar();
});
$("anotAnterior").addEventListener("click",
  () => irParaImagem(-1).catch(erroAviso));
$("anotPretag").addEventListener("click", prerotular);

$("anotVoltar").addEventListener("click", fecharAnotador);
function fecharAnotador() {
  $("anotador").hidden = true;
  segm.esquecer();
  $("anotImagem").src = "";
  anot.arrasto = null;
  $("anotCanvas").style.cursor = "crosshair";
  desenharGrade();
}

async function salvarAnotacao({ silencioso = false } = {}) {
  await api(`/projects/${estado.projeto}/samples/${anot.id}/annotation`,
            { method: "PUT",
              body: JSON.stringify({ boxes: anot.boxes, status: "submitted" }) });
  anot.sujo = false;
  // reflete na grade sem recarregar a página inteira
  const item = dados.itens[anot.indice];
  if (item) { item.annotation_id = item.annotation_id || "salva";
              item.n_caixas = anot.boxes.length; }
  if (!silencioso) avisar(`salvo · ${anot.boxes.length} caixa(s)`);
}

$("anotSalvar").addEventListener("click", async () => {
  // gravar com um contorno em rascunho o perderia sem aviso
  if (segmentacao() && segm.seg.rascunho)
    return avisar("há um contorno em rascunho — Enter aceita, Esc descarta", true);
  const botao = $("anotSalvar");
  botao.disabled = true;
  try {
    await salvarAnotacao();
    await irParaImagem(+1);
  } catch (e) { avisar(e.message, true); }
  finally { botao.disabled = false; }
});

/** Da tela de um dataset, ir direto rotular as imagens dele: troca de aba e
 *  já aplica o escopo, para o rotulador cair no subconjunto certo. */
export async function rotularDataset(slug) {
  dados.dataset = slug;
  dados.pagina = 0;
  dados.itens = [];                 // força o (re)carregamento com o novo escopo
  preencherEscopo();
  $("escopoDataset").value = slug;
  dados.dataset = slug;
  mostrarAba("dados");              // itens vazio → carrega uma vez, já no escopo
}
