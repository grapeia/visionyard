/* Aba Datasets: a definição, as origens, a prévia e as versões seladas. */

import { rotularDataset } from "./anotador.js";
import { abrirOrigem } from "./bucket.js";
import { $, api, avisar, comTravaDeEnvio, esc, estado, num, post } from "./nucleo.js";
import { abrirRoi, pctRoi } from "./roi.js";

export async function carregarDatasets() {
  estado.datasets = await api(`/projects/${estado.projeto}/datasets`);
  const ul = $("listaDatasets");
  ul.innerHTML = estado.datasets.map((d) => `
    <li><button data-ds="${esc(d.slug)}" aria-current="${d.slug === estado.atual}">
      ${esc(d.name)}
      <span class="qtd">${d.versoes ? "v" + d.ultima_versao : "—"}</span>
    </button></li>`).join("");

  if (!estado.datasets.length) {
    estado.atual = null;
    $("painelDataset").innerHTML = `
      <div class="vazio">
        <p>Nenhum dataset neste projeto.</p>
        <p class="dica">Um dataset reúne origens do bucket e produz versões congeladas,
           que é o que torna dois treinos comparáveis.</p>
        <button class="primario" id="botaoPrimeiroDataset">Criar o primeiro</button>
      </div>`;
    $("botaoPrimeiroDataset").addEventListener(
      "click", () => $("dlgDataset").showModal());
    return;
  }
  await abrirDataset(estado.atual || estado.datasets[0].slug);
}

$("listaDatasets").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-ds]");
  if (b) abrirDataset(b.dataset.ds).catch(() => {});
});

export async function abrirDataset(slug) {
  estado.atual = slug;
  document.querySelectorAll("[data-ds]").forEach((b) =>
    b.setAttribute("aria-current", b.dataset.ds === slug));
  $("painelDataset").innerHTML = `<p class="carregando">Carregando…</p>`;

  const base = `/projects/${estado.projeto}/datasets/${slug}`;
  try {
    const [ds, previa] = await Promise.all([api(base), api(`${base}/preview`)]);
    estado.previa = previa;
    render(ds, previa);
  } catch (e) {
    // Sem isto o painel fica em "Carregando…" para sempre e nenhum clique
    // seguinte recupera.
    $("painelDataset").innerHTML =
      `<div class="vazio"><p>Não consegui abrir este dataset.</p>
       <p class="dica">${esc(e.message)}</p>
       <button id="botaoTentarDeNovo">Tentar de novo</button></div>`;
    $("botaoTentarDeNovo").addEventListener("click", () => abrirDataset(slug));
  }
}

function render(ds, previa) {
  // imagem sem md5 barra o congelamento inteiro no servidor: sem isto o botão
  // ficava ativo e todo clique voltava 409
  const podeCongelar = previa.entrariam > 0 && !previa.sem_md5;
  $("painelDataset").innerHTML = `
    <div class="cabeca">
      <h1>${esc(ds.name)}</h1>
      <span class="tipo-ds ${esc(ds.kind)}">${esc(rotuloTipo(ds.kind))}</span>
      <span class="espaco"></span>
      ${ds.pretags_rascunho ? `<button class="discreto ruim" id="botaoDescartarPretags"
        title="remover os pré-tags rascunho não corrigidos">Descartar ${num(ds.pretags_rascunho)} pré-tags</button>` : ""}
      <button id="botaoRotular">Rotular estas imagens</button>
      <button class="primario" id="botaoCongelar" ${podeCongelar ? "" : "disabled"}>
        Congelar v${previa.proxima_versao}</button>
    </div>

    <section class="secao">
      <header>
        <span class="rotulo">Recorte (ROI)</span>
        <span class="espaco"></span>
        <button id="botaoRoi">${ds.roi ? "Editar recorte" : "Configurar recorte"}</button>
      </header>
      ${ds.roi ? `<p class="dica">Recorta cada imagem para
         <strong>x ${pctRoi(ds.roi.x)}–${pctRoi(ds.roi.x + ds.roi.w)}%,
         y ${pctRoi(ds.roi.y)}–${pctRoi(ds.roi.y + ds.roi.h)}%</strong> do quadro,
         aplicado no treino, avaliação e inferência. As próximas versões
         congelam com este recorte.</p>`
        : `<p class="dica">Sem recorte: treina no quadro inteiro. Configure quando só
             um alvo, sempre na mesma faixa, deve ser detectado — o recorte tira os
             vizinhos idênticos que senão confundem o modelo.</p>`}
    </section>

    <section class="secao">
      <header>
        <span class="rotulo">Origens</span>
        <span class="espaco"></span>
        <button id="botaoNovaOrigem">Adicionar origem</button>
      </header>
      ${ds.origens.length ? `<ul class="origens">${ds.origens.map(linhaOrigem).join("")}</ul>`
        : `<div class="vazio"><p>Nenhuma origem ainda.</p>
             <p class="dica">Aponte para um prefixo do bucket para trazer imagens.</p></div>`}
    </section>

    <section class="secao">
      <header><span class="rotulo">Prévia</span></header>
      <div class="previa">
        ${numero(previa.imagens_nas_origens, "nas origens")}
        ${numero(previa.anotadas, "anotadas")}
        ${numero(previa.pendentes, "sem anotação", previa.pendentes > 0)}
        ${numero(previa.entrariam, `entram na v${previa.proxima_versao}`)}
      </div>
      ${previa.sem_dimensao ? `<p class="dica">${num(previa.sem_dimensao)} imagens ainda
         sem dimensão registrada — só é descoberta ao abrir o arquivo.</p>` : ""}
      ${previa.sem_md5 ? `<p class="dica ruim">${num(previa.sem_md5)} imagens sem md5
         (resquício de uma migração antiga). Sem identidade de conteúdo o agente não
         consegue baixá-las, então o congelamento fica bloqueado até que a origem
         delas saia deste dataset.</p>` : ""}
    </section>

    <section class="secao">
      <header><span class="rotulo">Versões</span></header>
      ${ds.versoes.length ? `<ul class="trilho">${ds.versoes.map(linhaVersao).join("")}</ul>`
        : `<div class="vazio"><p>Nenhuma versão congelada.</p>
             <p class="dica">Congelar fixa a divisão treino/validação e grava o manifesto.
                A partir daí, aquele conjunto nunca muda.</p></div>`}
    </section>`;

  $("botaoCongelar").addEventListener("click", abrirCongelar);
  $("botaoRoi").addEventListener("click", () => abrirRoi(ds));
  if (ds.pretags_rascunho)
    $("botaoDescartarPretags").addEventListener("click",
      () => descartarPretags(ds.slug, ds.pretags_rascunho));
  $("botaoRotular").addEventListener("click", () => rotularDataset(ds.slug));
  $("botaoNovaOrigem").addEventListener("click",
    () => abrirOrigem().catch((e) => avisar(e.message, true)));
  $("painelDataset").querySelectorAll("[data-remover]").forEach((b) =>
    b.addEventListener("click", () => removerOrigem(b.dataset.remover, b.dataset.uri)));
  $("painelDataset").querySelectorAll("[data-diff]").forEach((b) =>
    b.addEventListener("click", () => verDiff(Number(b.dataset.diff))));
}

const rotuloTipo = (k) =>
  ({ train: "Treino", benchmark: "Benchmark", eval: "Avaliação" }[k] || k);

const numero = (v, k, destaque = false) => `
  <span class="numero ${destaque ? "pendente" : ""}">
    <span class="v">${num(v)}</span><span class="k rotulo">${esc(k)}</span></span>`;

const linhaOrigem = (o) => `
  <li>
    <span class="fonte-tipo">${esc(o.kind)}</span>
    <span class="uri">${esc(o.uri)}</span>
    <span class="imagens">${num(o.imagens)} img</span>
    <button class="discreto" data-remover="${esc(o.id)}" data-uri="${esc(o.uri)}"
            title="Remover da definição">Remover</button>
  </li>`;

const linhaVersao = (v) => `
  <li class="versao">
    <div class="versao-topo">
      <span class="versao-n">v${v.version}</span>
      <span class="selo">selada</span>
      ${v.version > 1 ? `<button class="discreto" data-diff="${v.version}">ver o que mudou</button>` : ""}
    </div>
    <p class="versao-desc">${esc(v.descricao || "")}</p>
    <div class="versao-meta">
      <span>${num(v.stats.imagens)} imagens</span>
      <span>${num(v.stats.train)} treino · ${num(v.stats.val)} validação</span>
      <span>${esc(v.split_policy.method === "by_group" ? "por grupo" : "aleatória")}</span>
      <span>${esc((v.criado_por || "").split("@")[0])}</span>
      <span>${new Date(v.frozen_at).toLocaleString("pt-BR")}</span>
    </div>
  </li>`;

// --------------------------------------------------------------- ações ---

$("botaoNovoDataset").addEventListener("click", () => $("dlgDataset").showModal());

// Sugere o identificador a partir do nome, sem travar edição manual.
$("dsNome").addEventListener("input", (ev) => {
  const campo = $("dsSlug");
  if (campo.dataset.tocado) return;
  campo.value = ev.target.value.toLowerCase().normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "").replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
});
$("dsSlug").addEventListener("input", (ev) => { ev.target.dataset.tocado = "1"; });

$("formDataset").addEventListener("submit", comTravaDeEnvio($("formDataset"), async () => {
  try {
    await post(`/projects/${estado.projeto}/datasets`, {
      slug: $("dsSlug").value, name: $("dsNome").value, kind: $("dsTipo").value,
    });
    $("dlgDataset").close();
    estado.atual = $("dsSlug").value;
    $("formDataset").reset();
    delete $("dsSlug").dataset.tocado;
    avisar("Dataset criado.");
    await carregarDatasets();
  } catch (e) { avisar(e.message, true); }
}));

/** A importação roda como job; acompanha até terminar. */
export async function esperarJob(jobId) {
  const url = `/projects/${estado.projeto}/jobs/${jobId}`;
  for (let i = 0; i < 600; i++) {
    const j = await api(url);
    if (j.status === "succeeded") {
      const r = j.result || {};
      avisar(`${num(r.samples_created)} imagens novas · ${num(r.samples_existing)} já existiam`);
      return j;
    }
    if (j.status === "failed") throw new Error(j.error || "importação falhou");
    if (j.progress && j.progress.lidos) avisar(`${num(j.progress.lidos)} lidas…`);
    await new Promise((r) => setTimeout(r, 600));
  }
  throw new Error("importação demorou demais — acompanhe pelos jobs");
}

async function removerOrigem(id, uri) {
  const url = `/projects/${estado.projeto}/datasets/${estado.atual}/sources/${id}`;
  let imp;
  try { imp = await api(`${url}/impacto`); }
  catch (e) { avisar(e.message, true); return; }
  const efeito = imp.em_outro_dataset
    ? "Outro dataset também usa esta origem: as imagens continuam no projeto."
    : imp.imagens
      ? `${num(imp.imagens)} imagens saem do projeto` +
        (imp.anotacoes ? `, com ${num(imp.anotacoes)} anotações — sem volta.` : ".") +
        "\nOs arquivos no bucket não são tocados; adicionar a origem de novo" +
        " reimporta as imagens, mas as anotações feitas aqui não voltam."
      : "Nenhuma imagem sai do projeto: as desta origem continuam em uso" +
        " (versão congelada, ou outro dataset por esta ou por outra origem).";
  if (!confirm(`Remover ${uri} deste dataset?\n\n${efeito}\n\n` +
      "Versões já congeladas não mudam.")) return;
  try {
    await api(url, { method: "DELETE" });
    avisar(imp.imagens && !imp.em_outro_dataset
      ? `Origem removida · ${num(imp.imagens)} imagens fora do projeto.`
      : "Origem removida do dataset.");
    await abrirDataset(estado.atual);
  } catch (e) { avisar(e.message, true); }
}

async function descartarPretags(slug, n) {
  if (!confirm(`Descartar ${n} pré-tags (rascunho) deste dataset?\n\n` +
      "Remove só as caixas geradas pelo modelo que ninguém corrigiu. " +
      "Trabalho humano e pré-tags já corrigidos ficam intactos. Sem volta."))
    return;
  try {
    const r = await post(
      `/projects/${estado.projeto}/datasets/${slug}/prelabel/discard`, {});
    avisar(`${num(r.removidos)} pré-tags removidos.`);
    await abrirDataset(slug);
  } catch (e) { avisar(e.message, true); }
}

function abrirCongelar() {
  const p = estado.previa;
  $("congelarResumo").textContent =
    `${num(p.entrariam)} imagens entram na v${p.proxima_versao}. ` +
    `A divisão treino/validação é sorteada agora e fica gravada — ` +
    `é o que torna dois treinos comparáveis.`;
  $("dlgCongelar").showModal();
}

$("formCongelar").addEventListener("submit", comTravaDeEnvio($("formCongelar"), async () => {
  try {
    const r = await post(
      `/projects/${estado.projeto}/datasets/${estado.atual}/freeze`, {
        descricao: $("cgDesc").value,
        val_ratio: Number($("cgVal").value),
        split_method: $("cgSplit").value,
      });
    $("dlgCongelar").close();
    $("formCongelar").reset();
    avisar(`v${r.version} congelada · ${num(r.stats.imagens)} imagens`);
    await abrirDataset(estado.atual);
  } catch (e) { avisar(e.message, true); }
}));

async function verDiff(versao) {
  try {
    const d = await api(
      `/projects/${estado.projeto}/datasets/${estado.atual}/versions/${versao}/diff`);
    const lista = (t, arr) => arr.length
      ? `${t} (${arr.length}):\n` + arr.slice(0, 25).join("\n") +
        (arr.length > 25 ? `\n… e mais ${arr.length - 25}` : "")
      : `${t}: nenhuma`;
    alert(`v${d.de} → v${d.para}\n\n` +
          lista("Entraram", d.entraram) + "\n\n" + lista("Saíram", d.sairam));
  } catch (e) { avisar(e.message, true); }
}
