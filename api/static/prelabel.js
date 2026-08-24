/* Pré-tagueamento em lote, disparado da aba Rotulagem.
 *
 * Aqui o dataset é o ESCOPO já escolhido na barra; o que se escolhe é o modelo.
 * É a assistência do rotulador: os rascunhos caem na grade daquela mesma aba. */

import { $, RANK, api, avisar, comTravaDeEnvio, erroAviso, esc, estado, num, post } from "./nucleo.js";
import { dados } from "./rotulagem.js";

/** Mostra/habilita o botão de pré-tag conforme papel e escopo. */
export function atualizarBotaoPretag() {
  const b = $("botaoPreTagLote");
  b.hidden = RANK[estado.papel] < RANK.engineer;
  b.disabled = !dados.dataset;
  b.title = dados.dataset ? "rode um modelo sobre este dataset"
    : "escolha um dataset no escopo para pré-taguear";
}

$("botaoPreTagLote").addEventListener("click",
  () => abrirPreTagLote().catch(erroAviso));

async function abrirPreTagLote() {
  if (!dados.dataset) { avisar("escolha um dataset no escopo", true); return; }
  const ds = (estado.datasets || []).find((d) => d.slug === dados.dataset);
  $("plDatasetNome").textContent = ds ? ds.name : dados.dataset;
  $("dlgPrelabel").dataset.dataset = dados.dataset;
  const nPag = dados.itens.length;
  $("plEscopo").querySelector('[value="pagina"]').textContent =
    `Só esta página (${nPag} ${nPag === 1 ? "imagem" : "imagens"})`;
  $("plEscopo").value = "todo";
  const modelos = await api(`/projects/${estado.projeto}/models`).catch(() => []);
  if (!modelos.length) {
    avisar("nenhum modelo treinado ainda — treine um na aba Treinamento", true);
    return;
  }
  $("plModeloSel").innerHTML = modelos.map((m) => {
    const c = m.aliases && m.aliases.champion;
    return `<option value="${esc(m.slug)}">${esc(m.name)}${c ? " · champion v" + c : ""}</option>`;
  }).join("");
  await preencherPretagVersoes();
  $("dlgPrelabel").showModal();
}

$("plModeloSel").addEventListener("change",
  () => preencherPretagVersoes().catch((e) => avisar(e.message, true)));

async function preencherPretagVersoes() {
  const slug = $("plModeloSel").value;
  const d = await api(`/projects/${estado.projeto}/models/${slug}`).catch(() => null);
  const champ = d && (d.aliases.find((a) => a.alias === "champion") || {}).version;
  $("plVersao").innerHTML =
    `<option value="">champion${champ ? " (v" + champ + ")" : " — nenhum, usa a última"}</option>` +
    (d ? d.versoes.map((v) => `<option value="${v.version}">v${v.version}</option>`).join("") : "");
}

$("formPrelabel").addEventListener("submit", comTravaDeEnvio($("formPrelabel"), async () => {
  const versao = $("plVersao").value;
  const corpo = {
    model_slug: $("plModeloSel").value,
    conf: Number($("plConf").value), only_missing: $("plMissing").checked,
  };
  if ($("plEscopo").value === "pagina") {
    corpo.sample_ids = dados.itens.map((it) => it.id);
    if (!corpo.sample_ids.length) { avisar("esta página está vazia", true); return; }
  } else {
    corpo.dataset_slug = $("dlgPrelabel").dataset.dataset;
  }
  if (versao) corpo.version = Number(versao);
  try {
    const r = await post(`/projects/${estado.projeto}/prelabel`, corpo);
    $("dlgPrelabel").close();
    avisar(`Pré-tag enfileirado (modelo v${r.model_version}). Aguardando uma máquina…`);
    acompanharPrelabel(r.job_id);
  } catch (e) { avisar(e.message, true); }
}));

/** Acompanha o job de pré-tag até terminar, avisando o progresso. Diferente do
 *  treino (que tem tela própria), aqui basta o toast — o resultado aparece na
 *  aba Rotulagem como rascunho. */
async function acompanharPrelabel(jobId) {
  const url = `/projects/${estado.projeto}/jobs/${jobId}`;
  for (let i = 0; i < 1200; i++) {
    let j;
    try { j = await api(url); } catch { return; }
    if (j.status === "succeeded") {
      const r = j.result || {};
      avisar(`Pré-tag pronto: ${num(r.pretags)} caixas em ${num(r.imagens)} imagens. ` +
             `Veja na aba Rotulagem.`);
      return;
    }
    if (j.status === "failed") { avisar(j.error || "pré-tag falhou", true); return; }
    if (j.status === "canceled") { avisar("pré-tag cancelado"); return; }
    if (j.progress && j.progress.feito)
      avisar(`Pré-tagueando… ${num(j.progress.feito)}/${num(j.progress.de)}`);
    await new Promise((r) => setTimeout(r, 1500));
  }
}
