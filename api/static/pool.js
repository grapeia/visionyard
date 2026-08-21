/* Aba Pool: a fila de tarefas e o cadastro de máquinas. */

import { $, api, avisar, comTravaDeEnvio, erroAviso, esc, post } from "./nucleo.js";

export async function carregarDevices() {
  let devs, fila;
  try { [devs, fila] = await Promise.all([api("/devices"), api("/jobs")]); }
  catch (e) { $("painelDevices").innerHTML =
    `<div class="vazio"><p>${esc(e.message)}</p></div>`; return; }
  const notaAgente = `<p class="dica">Cadastrar aqui não basta: cada máquina
    precisa do <b>serviço local (agente visionyard)</b> rodando nela, configurado com
    o token — é ele que se apresenta à API e puxa os trabalhos. Sem o agente
    ativo, a máquina fica "fora do ar" e não pega nada da fila.</p>`;
  $("painelDevices").innerHTML = notaAgente + (devs.length
    ? `<ul class="lista-devices">${devs.map(cartaoDevice).join("")}</ul>`
    : `<div class="vazio"><p>Nenhuma máquina cadastrada.</p>
        <p class="dica">Cadastre as máquinas que executam jobs — servidores de GPU
           para treino, dispositivos para inferência. Cada uma recebe um token
           para o agente.</p></div>`);
  $("painelTasks").innerHTML = seccaoFila(fila);
  ligarAcoesDevice();
}

// sub-abas do Pool: Máquinas (cadastro/estado) e Tasks (fila de trabalho)
$("subPool").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-sub]");
  if (!b) return;
  document.querySelectorAll("#subPool [data-sub]").forEach((x) =>
    x.setAttribute("aria-current", x === b));
  const tasks = b.dataset.sub === "tasks";
  $("painelDevices").hidden = tasks;
  $("painelTasks").hidden = !tasks;
  $("botaoNovoDevice").hidden = tasks;
});

// ---- fila de tarefas (o que espera máquina, o que roda onde) ----

const JOB_TIPO = { train: "treino", export: "compilação", evaluate: "avaliação",
                   prelabel: "pré-tag", import: "importação" };
const JOB_ESTADO = { queued: "na fila", claimed: "aceito", running: "rodando",
                     succeeded: "concluído", failed: "falhou", canceled: "cancelado" };
const JOB_ATIVO = ["queued", "claimed", "running"];

function seccaoFila(fila) {
  fila = fila || [];
  const ativos = fila.filter((j) => JOB_ATIVO.includes(j.status));
  const passados = fila.filter((j) => !JOB_ATIVO.includes(j.status)).slice(0, 10);
  const linha = (j) => {
    const cls = j.status === "failed" ? "ruim"
      : j.status === "running" || j.status === "claimed" ? "ok" : "neutro";
    const flag = j.cancel_requested && JOB_ATIVO.includes(j.status)
      ? " · cancelando…" : "";
    return `<tr>
      <td>${esc(JOB_TIPO[j.type] || j.type)}</td>
      <td class="mono">${esc(j.project)}</td>
      <td><span class="selo-run ${cls}">${esc(JOB_ESTADO[j.status] || j.status)}</span>${flag}</td>
      <td>${esc(j.device || j.target_device || "pool")}${
        !j.device && j.target_device ? " (fixado)" : ""}</td>
      <td class="mono">${j.attempts}/${j.max_attempts}</td>
      <td class="mono">${new Date(j.created_at).toLocaleString("pt-BR")}</td>
      <td>${JOB_ATIVO.includes(j.status) && !j.cancel_requested
        ? `<button class="discreto ruim" data-cancelar-job="${esc(j.id)}">cancelar</button>`
        : ""}</td></tr>`;
  };
  return `<section class="fila-jobs">
    <header><span class="rotulo">Fila de trabalho</span>
      <span class="espaco"></span>
      <span class="dica">${ativos.length} ativa(s)</span>
      <button class="discreto" id="atualizarFila">atualizar</button></header>
    ${ativos.length || passados.length
      ? `<table class="tab-versoes"><thead><tr><th>tarefa</th><th>projeto</th>
          <th>situação</th><th>máquina</th><th>tent.</th><th>criada</th><th></th>
          </tr></thead><tbody>${[...ativos, ...passados].map(linha).join("")}</tbody></table>`
      : `<p class="dica">Nada na fila.</p>`}
  </section>`;
}

async function cancelarJob(id) {
  if (!confirm("Cancelar esta tarefa? Se já estiver rodando, a máquina para " +
               "no próximo heartbeat.")) return;
  try {
    await post(`/jobs/${id}/cancel`, {});
    await carregarDevices();
  } catch (e) { avisar(e.message, true); }
}

const DEV_ESTADO = {
  idle: "ocioso", busy: "ocupado", offline: "fora do ar",
  draining: "drenando", disabled: "desativado",
};
function cartaoDevice(d) {
  const caps = Object.entries(d.capabilities || {})
    .map(([k, v]) => `${esc(k)}${v === true ? "" : "=" + esc(v)}`).join(" · ");
  const hb = d.last_heartbeat
    ? new Date(d.last_heartbeat).toLocaleString("pt-BR") : "nunca";
  return `<li class="device">
    <div class="device-topo">
      <span class="device-nome">${esc(d.name)}</span>
      <span class="mono slug">${esc(d.slug)}</span>
      <span class="selo-run ${d.status === "idle" || d.status === "busy" ? "ok"
        : d.status === "disabled" ? "ruim" : "neutro"}">${esc(DEV_ESTADO[d.status] || d.status)}</span>
      <span class="espaco"></span>
      ${d.rodando ? `<span class="dica">${d.rodando} job(s)</span>` : ""}
    </div>
    <div class="device-meta">
      <span>${esc(d.device_class)}</span>
      <span>${caps || "sem capacidades"}</span>
      <span>ping ${esc(hb)}</span>
    </div>
    <div class="device-acoes">
      <button class="discreto" data-token="${esc(d.slug)}">novo token</button>
      ${d.status === "disabled"
        ? `<button class="discreto" data-estado="idle" data-slug="${esc(d.slug)}">reativar</button>`
        : d.status === "draining"
          ? `<button class="discreto" data-estado="idle" data-slug="${esc(d.slug)}">reativar no pool</button>
             <button class="discreto ruim" data-estado="disabled" data-slug="${esc(d.slug)}">desativar</button>`
          : `<button class="discreto" data-estado="draining" data-slug="${esc(d.slug)}">tirar do pool</button>
             <button class="discreto ruim" data-estado="disabled" data-slug="${esc(d.slug)}">desativar</button>`}
      <button class="discreto ruim" data-excluir="${esc(d.slug)}">excluir</button>
    </div>
  </li>`;
}

function ligarAcoesDevice() {
  $("painelDevices").querySelectorAll("[data-token]").forEach((b) =>
    b.addEventListener("click", () => rotacionarToken(b.dataset.token)));
  $("painelDevices").querySelectorAll("[data-estado]").forEach((b) =>
    b.addEventListener("click", () =>
      mudarEstadoDevice(b.dataset.slug, b.dataset.estado)));
  $("painelDevices").querySelectorAll("[data-excluir]").forEach((b) =>
    b.addEventListener("click", () => excluirDevice(b.dataset.excluir)));
  $("painelTasks").querySelectorAll("[data-cancelar-job]").forEach((b) =>
    b.addEventListener("click", () => cancelarJob(b.dataset.cancelarJob)));
  const at = document.getElementById("atualizarFila");
  if (at) at.addEventListener("click", () => carregarDevices().catch(erroAviso));
}

async function excluirDevice(slug) {
  if (!confirm(`Excluir a máquina "${slug}"? Não dá pra desfazer.\n\n` +
      'Se ela já rodou algum job, o sistema recusa (o histórico de quem ' +
      'executou o quê seria perdido) — nesse caso use "desativar".'))
    return;
  try {
    await api(`/devices/${slug}`, { method: "DELETE" });
    avisar("Máquina excluída.");
    await carregarDevices();
  } catch (e) { avisar(e.message, true); }
}

function mostrarToken(slug, token) {
  const box = document.createElement("div");
  box.className = "token-box";
  box.innerHTML = `<b>Token de ${esc(slug)}</b> — copie agora, não aparece de novo:
    <code class="mono">${esc(token)}</code>
    <button class="discreto" id="copiarToken">copiar</button>`;
  $("painelDevices").prepend(box);
  $("copiarToken").addEventListener("click", () => {
    navigator.clipboard.writeText(token).then(() => avisar("Token copiado."));
  });
}

async function rotacionarToken(slug) {
  if (!confirm(`Gerar novo token para ${slug}? O token atual deixa de valer ` +
               "e o agente precisa ser reconfigurado.")) return;
  try {
    const r = await post(`/devices/${slug}/token`, {});
    mostrarToken(slug, r.token);
  } catch (e) { avisar(e.message, true); }
}

async function mudarEstadoDevice(slug, estadoNovo) {
  if (estadoNovo === "disabled" &&
      !confirm(`Desativar ${slug}? O token para de valer e a máquina sai do pool.`))
    return;
  try {
    await post(`/devices/${slug}/status?estado=${estadoNovo}`, {});
    await carregarDevices();
  } catch (e) { avisar(e.message, true); }
}

$("botaoNovoDevice").addEventListener("click", () => $("dlgDevice").showModal());

$("devNome").addEventListener("input", (ev) => {
  const campo = $("devSlug");
  if (campo.dataset.tocado) return;
  campo.value = ev.target.value.toLowerCase().normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "").replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
});
$("devSlug").addEventListener("input", (ev) => { ev.target.dataset.tocado = "1"; });

$("formDevice").addEventListener("submit", comTravaDeEnvio($("formDevice"), async () => {
  const caps = {};
  $("devFuncoes").querySelectorAll("input:checked")
    .forEach((c) => { caps[c.value] = true; });
  if (!Object.keys(caps).length)
    return avisar("marque ao menos uma função", true);
  try {
    const r = await post("/devices", {
      slug: $("devSlug").value, name: $("devNome").value,
      device_class: $("devClasse").value, capabilities: caps,
    });
    $("dlgDevice").close();
    $("formDevice").reset();
    delete $("devSlug").dataset.tocado;
    await carregarDevices();
    mostrarToken(r.slug, r.token);
  } catch (e) { avisar(e.message, true); }
}));
