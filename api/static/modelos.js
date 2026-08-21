/* Aba Modelos: o registro de versões, os apelidos, compilação e avaliação.
 *
 * Versão é imutável (pesos + métricas) e o apelido é um ponteiro para ela.
 * Promover é mover o ponteiro (champion/challenger/baseline), não tocar na
 * versão — dá para voltar atrás sem reconstruir nada. */

import { $, RANK, api, avisar, comTravaDeEnvio, css, erroAviso, esc, estado, num, post } from "./nucleo.js";
import { tipoProjeto } from "./projeto.js";

const ALIASES_MODELO = ["champion", "challenger", "baseline"];
export const modelos = { atual: null };

export async function carregarModelos() {
  const lista = await api(`/projects/${estado.projeto}/models`);
  modelos.lista = lista;
  $("listaModelos").innerHTML = lista.length
    ? lista.map((m) => `
        <li><button data-modelo="${esc(m.slug)}" aria-current="${m.slug === modelos.atual}">
          ${esc(m.name)}
          <span class="qtd">${m.ultima ? "v" + m.ultima : "—"}</span>
        </button></li>`).join("")
    : "";
  $("listaModelos").querySelectorAll("[data-modelo]").forEach((b) =>
    b.addEventListener("click", () => abrirModelo(b.dataset.modelo).catch(erroAviso)));

  if (!lista.length) {
    modelos.atual = null;
    $("painelModelo").innerHTML = `<div class="vazio">
      <p>Nenhum modelo neste projeto ainda.</p>
      <p class="dica">Um modelo nasce de um treino concluído (aba Treinamento):
        o peso vira uma versão registrada aqui.</p></div>`;
    return;
  }
  await abrirModelo(modelos.atual || lista[0].slug);
}

async function abrirModelo(slug) {
  modelos.atual = slug;
  document.querySelectorAll("[data-modelo]").forEach((b) =>
    b.setAttribute("aria-current", b.dataset.modelo === slug));
  $("painelModelo").innerHTML = `<p class="carregando">Carregando…</p>`;
  const d = await api(`/projects/${estado.projeto}/models/${slug}`);
  const podeProm = RANK[estado.papel] >= RANK.engineer;
  // apelido vigente por versão, para marcar na tabela
  const aliasDe = {};
  d.aliases.forEach((a) => (aliasDe[a.version] = aliasDe[a.version] || []).push(a.alias));

  $("painelModelo").innerHTML = `
    <div class="cabeca">
      <h1>${esc(d.name)}</h1>
      <span class="tipo-ds">${esc(tipoProjeto(d.task_type))}</span>
      <span class="espaco"></span>
      ${d.aliases.map((a) =>
        `<span class="selo-alias ${esc(a.alias)}">${esc(a.alias)} → v${a.version}</span>`).join("")}
    </div>
    <section class="secao">
      <header><span class="rotulo">Versões</span></header>
      <table class="tab-versoes">
        <thead><tr><th>versão</th><th>mAP50-95</th><th>origem</th>
          <th>tamanho</th><th>builds</th><th>quando</th>${podeProm ? "<th>ações</th>" : ""}</tr></thead>
        <tbody>${d.versoes.map((v) => linhaModeloVersao(v, aliasDe[v.version], podeProm)).join("")}</tbody>
      </table>
    </section>
    ${seccaoComparacoes(d.versoes)}`;

  desenharComparacoes(d.versoes);

  if (podeProm) {
    $("painelModelo").querySelectorAll("[data-prom]").forEach((b) =>
      b.addEventListener("click", () =>
        promover(slug, b.dataset.alias, Number(b.dataset.prom))));
    $("painelModelo").querySelectorAll("[data-export]").forEach((b) =>
      b.addEventListener("click", () => abrirExport(slug, Number(b.dataset.export))));
    $("painelModelo").querySelectorAll("[data-deploy]").forEach((b) =>
      b.addEventListener("click", () => {
        const v = d.versoes.find((x) => x.version === Number(b.dataset.deploy));
        abrirDeploy(slug, v);
      }));
    $("painelModelo").querySelectorAll("[data-eval]").forEach((b) =>
      b.addEventListener("click", () => {
        const v = d.versoes.find((x) => x.version === Number(b.dataset.eval));
        abrirEvaluate(slug, v);
      }));
  }
  renderImplantacoes(slug).catch(() => {});
}

/** Badge de avaliação: benchmark e o mAP medido nele. Verde-selo para
 *  distinguir de build (laranja) — é métrica, não artefato. */
function badgeAval(a) {
  const m = a.metrics || {};
  const map = m["mAP50-95"] != null ? Number(m["mAP50-95"]).toFixed(3) : "—";
  return `<span class="badge-aval" title="benchmark ${esc(a.benchmark)} v${a.benchmark_version}">
    ${esc(a.benchmark)}·${map}</span>`;
}

/** Seção de implantações do projeto: device → modelo vN (formato) + estado. */
async function renderImplantacoes(slug) {
  const deps = await api(`/projects/${estado.projeto}/deployments`);
  // o usuário pode ter trocado de modelo durante o fetch: não anexa no painel
  // de outro modelo
  if (modelos.atual !== slug || !deps.length) return;
  const sec = document.createElement("section");
  sec.className = "secao";
  sec.innerHTML = `<header><span class="rotulo">Implantações</span></header>
    <ul class="lista-deploy">${deps.map((d) => `
      <li><span class="mono">${esc(d.device)}</span>
        <span>${esc(d.model_slug)} v${d.version} · ${esc(d.format)}</span>
        <span class="selo-run ${d.state === "active" ? "ok"
          : d.state === "failed" ? "ruim" : "espera"}">${esc(estadoDeploy(d.state))}</span>
        ${d.error ? `<span class="dica">${esc(d.error)}</span>` : ""}</li>`).join("")}</ul>`;
  $("painelModelo").appendChild(sec);
}

const estadoDeploy = (s) =>
  ({ pending: "aguardando", active: "ativo", failed: "falhou" }[s] || s);

async function abrirDeploy(modeloSlug, versao) {
  $("dpModelo").textContent = `${modeloSlug} v${versao.version}`;
  $("dlgDeploy").dataset.modelo = modeloSlug;
  $("dlgDeploy").dataset.version = versao.version;
  // formatos disponíveis = os builds daquela versão
  $("dpFormato").innerHTML = (versao.builds || []).map((b) =>
    `<option value="${esc(b.format)}">${esc(b.format)}${
      b.target && b.target.precision ? " · " + esc(b.target.precision) : ""}</option>`).join("");
  const devs = await api("/deploy-devices").catch(() => []);
  if (!devs.length) {
    avisar("nenhuma máquina cadastrada para receber a implantação", true);
    return;
  }
  $("dpDevice").innerHTML = devs.map((d) =>
    `<option value="${esc(d.slug)}">${esc(d.name)} (${esc(d.status)})</option>`).join("");
  $("dlgDeploy").showModal();
}

$("formDeploy").addEventListener("submit", comTravaDeEnvio($("formDeploy"), async () => {
  const slug = $("dlgDeploy").dataset.modelo;
  try {
    const r = await post(`/projects/${estado.projeto}/models/${slug}/deploy`, {
      version: Number($("dlgDeploy").dataset.version),
      format: $("dpFormato").value, device_slug: $("dpDevice").value,
    });
    $("dlgDeploy").close();
    avisar(`Implantação pedida em ${r.device}. A máquina puxa e ativa.`);
    if (modelos.atual === slug) await abrirModelo(slug).catch(() => {});
  } catch (e) { avisar(e.message, true); }
}));

/** Badge de um build: formato, precisão e a latência medida no alvo. */
function badgeBuild(b) {
  const t = b.target || {};
  const lat = b.bench && b.bench.p50_ms != null ? ` · ${b.bench.p50_ms}ms` : "";
  const rot = `${esc(b.format)}${t.precision ? " " + esc(t.precision) : ""}${
    t.dynamic ? " din" : ""}${lat}`;
  return `<span class="badge-build" title="${esc(t.device_class || "")}${
    b.built_on ? " · " + esc(b.built_on) : ""}">${rot}</span>`;
}

function linhaModeloVersao(v, aliases, podeProm) {
  const m = v.metrics || {};
  const map = m["mAP50-95"] != null ? Number(m["mAP50-95"]).toFixed(4) : "—";
  const origem = v.experimento
    ? `${esc(v.experimento)} · ${esc(v.dataset || "")} v${v.dataset_version || "?"}`
    : "—";
  const mb = v.bytes ? (v.bytes / 1e6).toFixed(1) + " MB" : "—";
  const selos = (aliases || []).map((a) =>
    `<span class="selo-alias ${esc(a)}">${esc(a)}</span>`).join(" ");
  const artefatos = [...(v.builds || []).map(badgeBuild),
                     ...(v.avaliacoes || []).map(badgeAval)].join(" ") || "—";
  const acoes = podeProm ? `<td class="prom-cel">${ALIASES_MODELO.map((a) =>
    `<button class="discreto" data-prom="${v.version}" data-alias="${a}"
       title="apontar ${a} para v${v.version}">${a[0].toUpperCase()}</button>`).join("")}
    <button class="discreto" data-eval="${v.version}" title="avaliar num benchmark">◎</button>
    <button class="discreto" data-export="${v.version}" title="compilar (onnx/engine)">⤓</button>
    ${(v.builds || []).length ? `<button class="discreto" data-deploy="${v.version}"
       title="implantar num dispositivo">↗</button>` : ""}</td>` : "";
  return `<tr>
    <td class="mono">v${v.version} ${selos}</td>
    <td class="mono">${map}</td>
    <td>${origem}</td>
    <td class="mono">${mb}</td>
    <td>${artefatos}</td>
    <td>${new Date(v.created_at).toLocaleDateString("pt-BR")}</td>
    ${acoes}</tr>`;
}

async function promover(slug, alias, version) {
  try {
    await api(`/projects/${estado.projeto}/models/${slug}/aliases/${alias}`,
              { method: "PUT", body: JSON.stringify({ version }) });
    avisar(`${alias} → v${version}`);
    await abrirModelo(slug);
    await carregarModelos();
  } catch (e) { avisar(e.message, true); }
}

// ---- export / compilação (a partir da aba Modelos) ----

function abrirExport(modeloSlug, version) {
  $("exModelo").textContent = `${modeloSlug} v${version}`;
  $("dlgExport").dataset.modelo = modeloSlug;
  $("dlgExport").dataset.version = version;
  alternarEngineOpts();
  $("dlgExport").showModal();
}

function alternarEngineOpts() {
  // opções de engine (precisão/DLA/dinâmico) só fazem sentido para engine
  $("exEngineOpts").style.display = $("exFormato").value === "engine" ? "" : "none";
}
$("exFormato").addEventListener("change", alternarEngineOpts);

// DLA compila com shape fixo; fora do DLA, dinâmico é o recomendado (igual ao .pt)
$("exDla").addEventListener("change", () => {
  $("exDyn").disabled = $("exDla").checked;
  $("exDyn").checked = !$("exDla").checked;
});

$("formExport").addEventListener("submit", comTravaDeEnvio($("formExport"), async () => {
  const slug = $("dlgExport").dataset.modelo;
  const version = Number($("dlgExport").dataset.version);
  const corpo = { format: $("exFormato").value, imgsz: Number($("exImg").value) };
  if (corpo.format === "engine") {
    corpo.precision = $("exPrec").value;
    corpo.dla = $("exDla").checked;
    corpo.dynamic = $("exDyn").checked;
  }
  try {
    const r = await post(
      `/projects/${estado.projeto}/models/${slug}/versions/${version}/export`, corpo);
    $("dlgExport").close();
    avisar(`Compilação enfileirada (${corpo.format}). Aguardando uma máquina…`);
    acompanharExport(r.job_id, slug);
  } catch (e) { avisar(e.message, true); }
}));

/** Acompanha o job de export; ao terminar, recarrega o modelo para o build
 *  aparecer na versão. */
async function acompanharExport(jobId, modeloSlug) {
  const url = `/projects/${estado.projeto}/jobs/${jobId}`;
  for (let i = 0; i < 1200; i++) {
    let j;
    try { j = await api(url); } catch { return; }
    if (j.status === "succeeded") {
      const r = j.result || {};
      const lat = r.bench && r.bench.p50_ms != null ? ` · ${r.bench.p50_ms}ms` : "";
      avisar(`Build pronto (${r.format}${lat}).`);
      if (modelos.atual === modeloSlug && !$("abaModelos").hidden)
        await abrirModelo(modeloSlug).catch(() => {});
      return;
    }
    if (j.status === "failed") { avisar(j.error || "compilação falhou", true); return; }
    if (j.status === "canceled") { avisar("compilação cancelada"); return; }
    await new Promise((r) => setTimeout(r, 1500));
  }
}

// ---- avaliação (a partir da aba Modelos) ----

async function abrirEvaluate(modeloSlug, versao) {
  $("avModelo").textContent = `${modeloSlug} v${versao.version}`;
  $("dlgEvaluate").dataset.modelo = modeloSlug;
  $("dlgEvaluate").dataset.version = versao.version;
  // datasets com versão congelada servem de benchmark
  const comVersao = (estado.datasets || []).filter((d) => d.versoes > 0);
  if (!comVersao.length) {
    avisar("congele uma versão de dataset para usar como benchmark", true);
    return;
  }
  // artefatos a medir: peso + cada build compilado, e o "todos" para comparar
  const builds = versao.builds || [];
  const opts = [`<option value="todos">Todos os formatos (comparar)</option>`,
                `<option value="pt;;">peso (.pt)</option>`];
  for (const b of builds) {
    const t = b.target || {};
    const rot = b.format + (t.precision ? " " + t.precision : "") +
      (t.dynamic ? " din" : "") + (t.dla ? " DLA" : "");
    opts.push(`<option value="${esc(b.format)};${esc(t.precision || "")};${t.dla ? "1" : ""}">${esc(rot)}</option>`);
  }
  $("avArtefato").innerHTML = opts.join("");
  $("avDataset").innerHTML = comVersao.map((d) =>
    `<option value="${esc(d.slug)}">${esc(d.name)}</option>`).join("");
  $("dlgEvaluate").showModal();
  await preencherBenchVersoes().catch((e) => avisar(e.message, true));
}

/** Descreve os artefatos a avaliar a partir da opção escolhida. */
function artefatosEscolhidos() {
  const v = $("avArtefato").value;
  const parse = (s) => {
    const [format, precision, dla] = s.split(";");
    const o = { format };
    if (precision) o.precision = precision;
    if (dla) o.dla = true;
    return o;
  };
  if (v !== "todos") return [parse(v)];
  return [...$("avArtefato").options].filter((o) => o.value !== "todos")
    .map((o) => parse(o.value));
}

$("avDataset").addEventListener("change",
  () => preencherBenchVersoes().catch((e) => avisar(e.message, true)));

async function preencherBenchVersoes() {
  const slug = $("avDataset").value;
  if (!slug) { $("avVersao").innerHTML = ""; return; }
  const d = await api(`/projects/${estado.projeto}/datasets/${slug}`);
  const sel = $("avVersao");
  sel.innerHTML = d.versoes.map((v) =>
    `<option value="${v.version}">v${v.version} — ${esc(v.descricao || "")}</option>`).join("");
  const info = () => {
    const v = d.versoes.find((x) => String(x.version) === sel.value);
    $("avInfo").textContent = v ? `${num(v.stats.imagens)} imagens no benchmark` : "";
  };
  sel.onchange = info; info();
}

$("formEvaluate").addEventListener("submit", comTravaDeEnvio($("formEvaluate"), async () => {
  const slug = $("dlgEvaluate").dataset.modelo;
  const version = $("dlgEvaluate").dataset.version;
  const base = { dataset_slug: $("avDataset").value,
                 dataset_version: Number($("avVersao").value) };
  const artefatos = artefatosEscolhidos();
  try {
    const jobs = [];
    for (const art of artefatos) {
      const r = await post(
        `/projects/${estado.projeto}/models/${slug}/versions/${version}/evaluate`,
        { ...base, ...art });
      jobs.push(r.job_id);
    }
    $("dlgEvaluate").close();
    avisar(artefatos.length > 1
      ? `${artefatos.length} avaliações enfileiradas (comparação). Aguardando máquina…`
      : "Avaliação enfileirada. Aguardando uma máquina…");
    // acompanha todas; cada uma recarrega o modelo ao concluir
    jobs.forEach((j) => acompanharAval(j, slug));
  } catch (e) { avisar(e.message, true); }
}));

async function acompanharAval(jobId, modeloSlug) {
  const url = `/projects/${estado.projeto}/jobs/${jobId}`;
  for (let i = 0; i < 1200; i++) {
    let j;
    try { j = await api(url); } catch { return; }
    if (j.status === "succeeded") {
      const m = (j.result || {}).metrics || {};
      avisar(`Avaliação pronta: mAP50-95 ${m["mAP50-95"] != null ? Number(m["mAP50-95"]).toFixed(4) : "—"}.`);
      if (modelos.atual === modeloSlug && !$("abaModelos").hidden)
        await abrirModelo(modeloSlug).catch(() => {});
      return;
    }
    if (j.status === "failed") { avisar(j.error || "avaliação falhou", true); return; }
    if (j.status === "canceled") { avisar("avaliação cancelada"); return; }
    await new Promise((r) => setTimeout(r, 1500));
  }
}

// ---- comparação de avaliações (pt × engine × DLA: mAP + pré/inf/pós) ----

function rotuloAval(a) {
  const t = a.target || {};
  return (a.format || "pt") + (t.precision ? " " + t.precision : "") +
         (t.dynamic ? " din" : "") + (t.dla ? " DLA" : "");
}

/** Agrupa as avaliações por (versão, benchmark) — cada grupo é uma comparação
 *  entre artefatos (pt/onnx/engine/DLA) no mesmo conjunto. */
function gruposAval(versoes) {
  const grupos = [];
  for (const v of versoes || []) {
    const porBench = {};
    for (const a of (v.avaliacoes || [])) {
      const k = `${a.benchmark}@v${a.benchmark_version}`;
      (porBench[k] = porBench[k] || []).push(a);
    }
    Object.entries(porBench).forEach(([k, avs], i) => grupos.push({
      id: `cmp-${v.version}-${i}`, titulo: `v${v.version} · benchmark ${k}`,
      rows: avs.map((a) => {
        const m = a.metrics || {};
        const pre = m.pre_ms || 0, inf = m.inf_ms || 0, pos = m.pos_ms || 0;
        return { rotulo: rotuloAval(a), map: m["mAP50-95"], map50: m["mAP50"],
                 pre, inf, pos, total: pre + inf + pos };
      }),
    }));
  }
  return grupos;
}

function seccaoComparacoes(versoes) {
  const grupos = gruposAval(versoes);
  if (!grupos.length) return "";
  return `<section class="secao">
    <header><span class="rotulo">Comparação de avaliações</span>
      <span class="espaco"></span>
      <span class="legenda"><i style="background:#3aa0ff"></i>pré
        <i style="background:#ff8a1f"></i>inferência
        <i style="background:#2bc47d"></i>pós</span></header>
    ${grupos.map((g) => `<div class="cmp-grupo">
      <span class="cmp-titulo">${esc(g.titulo)}</span>
      <table class="tab-versoes cmp-tab"><thead><tr><th>artefato</th>
        <th>mAP50-95</th><th>mAP50</th><th>pré</th><th>inf</th><th>pós</th>
        <th>total</th></tr></thead>
        <tbody>${g.rows.map(linhaAval).join("")}</tbody></table>
      <canvas class="cmp-chart" id="${g.id}" width="640"
        height="${Math.max(60, g.rows.length * 34 + 16)}"></canvas>
    </div>`).join("")}</section>`;
}

function linhaAval(r) {
  const ms = (x) => x ? x.toFixed(1) : "—";
  const mp = (x) => x != null ? Number(x).toFixed(3) : "—";
  return `<tr><td class="mono">${esc(r.rotulo)}</td><td class="mono">${mp(r.map)}</td>
    <td class="mono">${mp(r.map50)}</td><td class="mono">${ms(r.pre)}</td>
    <td class="mono">${ms(r.inf)}</td><td class="mono">${ms(r.pos)}</td>
    <td class="mono">${r.total ? r.total.toFixed(1) + "ms" : "—"}</td></tr>`;
}

function desenharComparacoes(versoes) {
  for (const g of gruposAval(versoes)) {
    const cv = document.getElementById(g.id);
    if (cv) desenharBarras(cv, g.rows);
  }
}

/** Barras horizontais empilhadas: pré + inferência + pós por artefato, largura
 *  ∝ ms. É o "onde o tempo é gasto" — DLA/engine encurtam a inferência. */
function desenharBarras(cv, rows) {
  const cx = cv.getContext("2d");
  cx.clearRect(0, 0, cv.width, cv.height);
  const padL = 96, padR = 66, top = 6, h = 22, gap = 12;
  const maxTotal = Math.max(0.001, ...rows.map((r) => r.total));
  const escala = (cv.width - padL - padR) / maxTotal;
  const cores = ["#3aa0ff", "#ff8a1f", "#2bc47d"];
  cx.font = "11px ui-monospace, monospace";
  rows.forEach((r, i) => {
    const y = top + i * (h + gap);
    cx.fillStyle = css("--texto-fraco") || "#8fa3b3";
    cx.textAlign = "right"; cx.fillText(r.rotulo, padL - 6, y + h / 2 + 4);
    let x = padL;
    [r.pre, r.inf, r.pos].forEach((seg, k) => {
      const w = seg * escala;
      cx.fillStyle = cores[k]; cx.fillRect(x, y, Math.max(0, w), h); x += w;
    });
    cx.fillStyle = css("--texto") || "#e6edf3"; cx.textAlign = "left";
    cx.fillText(r.total ? r.total.toFixed(1) + "ms" : "sem tempo", x + 6, y + h / 2 + 4);
  });
}
