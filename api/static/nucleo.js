/* O que todo módulo usa: o atalho do DOM, o estado do app, a porta HTTP, o
 * aviso da barra inferior e os escapes.
 *
 * Não importa de ninguém, de propósito: é a raiz do grafo, e é o que garante
 * que o `$` já exista quando cada módulo liga os seus listeners ao ser
 * avaliado. `mostrarEntrada` mora aqui pelo mesmo motivo — é a resposta do
 * `api()` ao 401, e deixá-la na tela de entrada faria um ciclo em que a
 * entrada é avaliada antes do `$`. */

export const $ = (id) => document.getElementById(id);

export const estado = { usuario: null, projeto: null, datasets: [], atual: null };

// ------------------------------------------------------------------ rede --

export async function api(caminho, opcoes = {}) {
  const r = await fetch("/v1" + caminho, {
    credentials: "same-origin",
    headers: opcoes.body ? { "Content-Type": "application/json" } : {},
    ...opcoes,
  });
  if (r.status === 401) { mostrarEntrada(); throw new Error("sessão expirada"); }
  if (r.status === 204) return null;
  const corpo = await r.json().catch(() => null);
  if (!r.ok) throw new Error(detalhe(corpo) || `erro ${r.status}`);
  return corpo;
}

/** FastAPI devolve `detail` como texto ou como lista de erros de validação. */
function detalhe(corpo) {
  const d = corpo && corpo.detail;
  if (!d) return null;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((e) => e.msg || "").filter(Boolean).join("; ");
  return null;
}

export const post = (c, corpo) => api(c, { method: "POST", body: JSON.stringify(corpo) });

export function avisar(texto, ruim = false) {
  const el = $("aviso");
  el.textContent = texto;
  el.classList.toggle("ruim", ruim);
  el.classList.add("mostra");
  clearTimeout(avisar._t);
  avisar._t = setTimeout(() => el.classList.remove("mostra"), 3800);
}

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const num = (n) => Number(n ?? 0).toLocaleString("pt-BR");

export function mostrarEntrada() {
  // Um <dialog> aberto por showModal() deixa o resto do documento inerte —
  // inclusive a tela de login que estamos prestes a mostrar. Sem fechar, a
  // sessão expirando durante um modal trava a página até dar F5.
  document.querySelectorAll("dialog[open]").forEach((d) => d.close());
  $("telaApp").hidden = true;
  $("telaEntrada").hidden = false;
  $("email").focus();
}

/** Impede que o duplo clique dispare a ação duas vezes.
 *
 * Congelar é a operação mais lenta (resolve origens, sorteia split, grava
 * manifesto) e a mais cara de repetir: duas chamadas criam duas versões de um
 * objeto que o produto trata como imutável e auditável. */
export function comTravaDeEnvio(form, acao) {
  return async (ev) => {
    ev.preventDefault();
    const botao = form.querySelector('button[value="ok"]');
    if (form.dataset.enviando) return;
    form.dataset.enviando = "1";
    if (botao) botao.disabled = true;
    try { await acao(ev); }
    finally { delete form.dataset.enviando; if (botao) botao.disabled = false; }
  };
}

export const erroAviso = (e) => avisar(e.message, true);

const CORES = ["#00d26a", "#ff8a1f", "#3aa0ff", "#e5484d", "#c77dff",
               "#ffd60a", "#00c2c7", "#ff6fb5"];
export const cor = (i) => CORES[i % CORES.length];

export const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
export const clamp01 = (v) => clamp(v, 0, 1);

export const css = (v) =>
  getComputedStyle(document.documentElement).getPropertyValue(v).trim();

export const RANK = { viewer: 0, annotator: 1, engineer: 2, admin: 3, owner: 4 };

/** Nome do papel na tela. O banco e a API guardam a chave em inglês; quem lê é
 *  português. Chave que não estiver aqui aparece como veio. */
export const PAPEL = { viewer: "Visualizador", annotator: "Anotador",
                       engineer: "Engenheiro", admin: "Administrador",
                       owner: "Dono" };
export const nomePapel = (p) => PAPEL[p] || p || "—";
