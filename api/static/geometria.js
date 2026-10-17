/* Geometria dos contornos: área, ponto dentro, simplificação e os recortes
 * (fatiar a principal, cortar pelo contorno). Sem DOM e sem import: é o que
 * permite exercitar estas funções fora do navegador.
 *
 * Polígono aqui é `[[x,y], ...]` sem repetir o primeiro vértice no fim. As
 * operações de conjunto vêm do `polygon-clipping`, que o chamador carrega e
 * passa como `pc`: recorte de polígono côncavo por côncavo (a meia-carcaça
 * é côncava) não cabe num Sutherland–Hodgman. */

// Pedaço com menos área que isto (fração da imagem) é sobra numérica da
// linha que passou raspando, não região que alguém quis marcar.
export const AREA_MINIMA = 1e-6;

export function area(pts) {
  let s = 0;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++)
    s += pts[j][0] * pts[i][1] - pts[i][0] * pts[j][1];
  return Math.abs(s) / 2;
}

export function dentroDe(x, y, pts) {
  let dentro = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const [xi, yi] = pts[i], [xj, yj] = pts[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi)
      dentro = !dentro;
  }
  return dentro;
}

/** Douglas–Peucker numa linha aberta: mantém as pontas. */
export function linha(pts, eps) {
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

/** Saída do polygon-clipping (MultiPolygon) → anéis externos, sem o vértice
 *  repetido e sem os minúsculos. Os buracos ficam de fora: a anotação guarda
 *  um anel só, e o pedaço com buraco é o que contorna um defeito já marcado
 *  por cima — cobrir o buraco é o que se quer (as máscaras se sobrepõem). */
function aneis(multi) {
  return multi
    .map((poligono) => poligono[0].slice(0, -1))
    .filter((a) => a.length >= 3 && area(a) >= AREA_MINIMA);
}

const fechado = (pts) => [[...pts, pts[0]]];

/** O polígono cortado pelo contorno: o que passa da borda some. Pode virar
 *  mais de um pedaço (contorno côncavo) ou nenhum. */
export function recortarPor(pc, pts, contorno) {
  return aneis(pc.intersection(fechado(pts), fechado(contorno)));
}

/** Um dos dois lados da linha, como polígono: a linha esticada nas duas
 *  pontas até bem longe da imagem, e fechada por um arco grande. A linha que
 *  termina antes da borda corta como se continuasse reta — é o que permite
 *  arrastar sem precisar sair exatamente pelo outro lado. Quem limita o
 *  corte aos pedaços que o traço tocou é `fatiar`, não este polígono. */
function lado(traco) {
  const LONGE = 10, RAIO = 40, c = [0.5, 0.5];
  const sentido = (de, para) => {
    const dx = de[0] - para[0], dy = de[1] - para[1], n = Math.hypot(dx, dy) || 1;
    return [de[0] + (dx / n) * LONGE, de[1] + (dy / n) * LONGE];
  };
  const n = traco.length;
  const a = sentido(traco[0], traco[1]), b = sentido(traco[n - 1], traco[n - 2]);
  const ang = (p) => Math.atan2(p[1] - c[1], p[0] - c[0]);
  let de = ang(b);
  const ate = ang(a);
  while (de > ate) de -= 2 * Math.PI;     // arco crescente de b até a
  const arco = [];
  for (let t = de; t < ate; t += Math.PI / 18)
    arco.push([c[0] + RAIO * Math.cos(t), c[1] + RAIO * Math.sin(t)]);
  arco.push([c[0] + RAIO * Math.cos(ate), c[1] + RAIO * Math.sin(ate)]);
  return [a, ...traco, b, ...arco];
}

/** Quanto do traço passa por dentro de `pts`, em px (`escala` = [w, h] da
 *  tela). Por amostragem: cada trecho é medido em passos de ~2 px. */
export function comprimentoDentro(traco, pts, [w, h] = [1, 1]) {
  let total = 0;
  for (let i = 1; i < traco.length; i++) {
    const [ax, ay] = traco[i - 1], [bx, by] = traco[i];
    const l = Math.hypot((bx - ax) * w, (by - ay) * h);
    const n = Math.max(1, Math.ceil(l / 2));
    for (let k = 0; k < n; k++) {
      const t = (k + 0.5) / n;
      if (dentroDe(ax + (bx - ax) * t, ay + (by - ay) * t, pts)) total += l / n;
    }
  }
  return total;
}

/** Fatia os pedaços de uma principal com uma linha.
 *
 *  `pecas` são os recortes que ela já tem ({cls, pts}). O que da principal
 *  ainda não está em pedaço nenhum entra como pedaço sem classe — é assim
 *  que o primeiro corte divide a principal inteira, e os seguintes dividem
 *  os pedaços. Cada pedaço atravessado vira dois (ou mais), com a classe do
 *  original. Devolve `{ pecas, divididos }` — `pecas` é a lista nova
 *  inteira, na ordem das antigas; `divididos` zero quer dizer que a linha
 *  não atravessou nada, e aí nada muda.
 *
 *  Só se divide o pedaço por onde o traço passou — ao menos `minimo` px
 *  dentro dele. A linha esticada até o infinito cortaria também o vizinho:
 *  quem começa o traço perto de uma fatia já feita (às vezes um tico do
 *  outro lado dela) quer que o corte novo pare nela, em T. */
export function fatiar(pc, pecas, contorno, traco, { escala = [1, 1], minimo = 0 } = {}) {
  if (traco.length < 2) return { pecas, divididos: 0 };
  const meio = fechado(lado(traco));
  const sobra = pecas.length
    ? aneis(pc.difference(fechado(contorno), ...pecas.map((p) => fechado(p.pts))))
    : [contorno];
  const candidatas = [...pecas, ...sobra.map((pts) => ({ cls: null, pts, nova: true }))];
  const saida = [];
  let divididos = 0;
  for (const p of candidatas) {
    if (minimo > 0 && comprimentoDentro(traco, p.pts, escala) < minimo) {
      if (!p.nova) saida.push(p);
      continue;
    }
    const um = aneis(pc.intersection(fechado(p.pts), meio));
    const outro = aneis(pc.difference(fechado(p.pts), meio));
    if (!um.length || !outro.length) {
      if (!p.nova) saida.push(p);         // sobra não atravessada fica sem pedaço
      continue;
    }
    divididos++;
    for (const pts of [...um, ...outro]) saida.push({ ...p, pts, nova: undefined });
  }
  for (const p of saida) delete p.nova;
  return { pecas: divididos ? saida : pecas, divididos };
}

/** Um ponto garantidamente dentro do polígono — onde o gabarito guarda a
 *  semente da classe. O centroide serve no convexo; no côncavo (o pedaço em
 *  "C" que contorna uma reentrância) ele cai fora, e aí vale o meio do
 *  trecho mais largo da horizontal que passa por ele. */
export function pontoDentro(pts) {
  const n = pts.length;
  const cx = pts.reduce((s, v) => s + v[0], 0) / n;
  const cy = pts.reduce((s, v) => s + v[1], 0) / n;
  if (dentroDe(cx, cy, pts)) return [cx, cy];
  const ys = pts.map((v) => v[1]);
  const y0 = Math.min(...ys), y1 = Math.max(...ys);
  let melhor = null, largura = -1;
  // a horizontal do centroide primeiro; se ela não cruzar nada largo,
  // outras alturas
  for (const y of [cy, ...Array.from({ length: 9 }, (_, k) => y0 + ((k + 1) / 10) * (y1 - y0))]) {
    const xs = [];
    for (let i = 0, j = n - 1; i < n; j = i++) {
      const [xi, yi] = pts[i], [xj, yj] = pts[j];
      if ((yi > y) !== (yj > y)) xs.push(xi + ((y - yi) / (yj - yi)) * (xj - xi));
    }
    xs.sort((a, b) => a - b);
    for (let k = 0; k + 1 < xs.length; k += 2) {
      if (xs[k + 1] - xs[k] > largura) { largura = xs[k + 1] - xs[k]; melhor = [(xs[k] + xs[k + 1]) / 2, y]; }
    }
    if (melhor && y === cy) break;
  }
  return melhor || pts[0];
}

/** A classe de `peca` herdada de quem, em `referencia`, mais se sobrepõe a
 *  ela — é o que mantém a classe no lugar enquanto a linha do corte é
 *  arrastada e os pedaços são refeitos. Sem sobreposição, `undefined`. */
export function classePorSobreposicao(pc, peca, referencia) {
  let melhor, maior = 0;
  for (const r of referencia) {
    const s = pc.intersection([[...peca.pts, peca.pts[0]]], [[...r.pts, r.pts[0]]])
      .reduce((t, poligono) => t + area(poligono[0].slice(0, -1)), 0);
    if (s > maior) { maior = s; melhor = r.cls; }
  }
  return melhor;
}

/** A caixa do contorno, que é o quadro do gabarito: linhas e sementes
 *  guardadas em 0–1 dela servem para banda maior, menor ou deslocada. */
export function caixaDe(pts) {
  const xs = pts.map((v) => v[0]), ys = pts.map((v) => v[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

export const paraQuadro = ([x, y], [x0, y0, x1, y1]) =>
  [(x - x0) / ((x1 - x0) || 1), (y - y0) / ((y1 - y0) || 1)];

/** Do quadro para a imagem. `espelho` vira na horizontal: a meia-carcaça
 *  esquerda é o espelho da direita. */
export const doQuadro = ([u, v], [x0, y0, x1, y1], espelho = false) =>
  [x0 + (espelho ? 1 - u : u) * (x1 - x0), y0 + v * (y1 - y0)];
