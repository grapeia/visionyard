/* Aba Rotulagem: o escopo, os filtros e a grade de imagens.
 *
 * As imagens vêm assinadas e são buscadas direto do GCS: a API emite a URL e
 * não trafega bytes. O canvas de anotação fica no `anotador.js`. */

import { abrirAnotador } from "./anotador.js";
import { $, api, avisar, erroAviso, esc, estado, num } from "./nucleo.js";
import { atualizarBotaoPretag } from "./prelabel.js";

export const dados = { pagina: 0, filtro: "todas", dataset: "",
                itens: [], total: 0, assina: false };

export function preencherEscopo() {
  const sel = $("escopoDataset");
  const atual = dados.dataset;
  sel.innerHTML = `<option value="">Todas as imagens</option>` +
    estado.datasets.map((d) =>
      `<option value="${esc(d.slug)}">${esc(d.name)}</option>`).join("");
  // o dataset escolhido pode ter sumido (troca de projeto): volta p/ "todas"
  sel.value = estado.datasets.some((d) => d.slug === atual) ? atual : "";
  dados.dataset = sel.value;
  atualizarBotaoPretag();
}

$("escopoDataset").addEventListener("change", (ev) => {
  dados.dataset = ev.target.value;
  dados.pagina = 0;
  atualizarBotaoPretag();
  carregarDados().catch(erroAviso);
});

$("filtros").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-filtro]");
  if (!b) return;
  document.querySelectorAll("#filtros [data-filtro]").forEach((x) =>
    x.setAttribute("aria-current", x === b));
  dados.filtro = b.dataset.filtro;
  dados.pagina = 0;
  carregarDados().catch(erroAviso);
});

$("paginaAnterior").addEventListener("click", () => {
  if (dados.pagina > 0) { dados.pagina--; carregarDados().catch(erroAviso); }
});
$("paginaProxima").addEventListener("click", () => {
  dados.pagina++; carregarDados().catch(erroAviso);
});

export async function carregarDados() {
  $("grade").innerHTML = `<p class="carregando">Carregando…</p>`;
  const q = `?pagina=${dados.pagina}&filtro=${dados.filtro}` +
            (dados.dataset ? `&dataset=${encodeURIComponent(dados.dataset)}` : "");
  const d = await api(`/projects/${estado.projeto}/samples${q}`);
  Object.assign(dados, d, { itens: d.itens });

  $("contagemDados").textContent =
    `${num(d.total)} imagens · página ${d.pagina + 1}`;
  $("paginaAnterior").disabled = d.pagina === 0;
  $("paginaProxima").disabled = d.itens.length < d.por_pagina;

  if (!d.itens.length) {
    $("grade").innerHTML = `<div class="vazio"><p>Nenhuma imagem aqui.</p>
      <p class="dica">Catalogue um prefixo do bucket pela aba Datasets,
         em "Adicionar origem".</p></div>`;
    return;
  }
  $("grade").innerHTML = d.itens.map(cartao).join("");
  $("grade").querySelectorAll("[data-abrir]").forEach((b) =>
    b.addEventListener("click", () => abrirAnotador(b.dataset.abrir)));
  if (!d.assina) avisar("sem miniaturas: a credencial não assina URL", true);
}

function cartao(it) {
  const nome = it.uri.split("/").pop();
  // "0" é informação: significa "olhei e não há nada", não "não olhei".
  const estadoCaixa = it.annotation_id === null
    ? `<span class="marca-estado">—</span>`
    : (it.n_caixas === 0
        ? `<span class="marca-estado vazia">vazia</span>`
        : `<span class="marca-estado feita">${it.n_caixas}</span>`);
  return `<button class="cartao" data-abrir="${esc(it.id)}" title="${esc(nome)}">
    ${it.url ? `<img src="${esc(it.url)}" alt="" loading="lazy">`
             : `<span class="sem-imagem">sem miniatura</span>`}
    ${estadoCaixa}<span class="nome">${esc(nome)}</span></button>`;
}
