/* Navegador de bucket: escolher o prefixo que vira origem do dataset. */

import { abrirDataset, esperarJob } from "./datasets.js";
import { $, api, avisar, comTravaDeEnvio, esc, estado, post } from "./nucleo.js";

/** Abre o diálogo já com os buckets da instalação e na raiz do primeiro. */
export async function abrirOrigem() {
  const sel = $("orBucket");
  if (!sel.options.length) {
    const buckets = await api(`/projects/${estado.projeto}/buckets`);
    if (!buckets.length) return avisar("nenhum bucket configurado", true);
    sel.innerHTML = buckets
      .map((b) => `<option value="${esc(b.nome)}">${esc(b.nome)}</option>`).join("");
  }
  $("dlgOrigem").showModal();
  await navegar("");
}

/** Lista um nível do bucket. `delimiter` no servidor evita varrer tudo. */
async function navegar(prefixo) {
  const bucket = $("orBucket").value;
  $("orPrefixo").value = prefixo;
  $("orNavegador").innerHTML = `<p class="aviso-nav">Lendo…</p>`;
  let d;
  try {
    d = await api(`/projects/${estado.projeto}/buckets/browse` +
                  `?bucket=${encodeURIComponent(bucket)}` +
                  `&prefixo=${encodeURIComponent(prefixo)}`);
  } catch (e) {
    $("orNavegador").innerHTML =
      `<p class="aviso-nav">${esc(e.message)}</p>`;
    return;
  }
  desenharCaminho(d);

  const miniaturas = d.objetos.filter((o) => o.url).slice(0, 12);
  const itens = [];
  for (const p of d.pastas) {
    const nome = p.slice(prefixo.length);
    itens.push(`<li><button type="button" class="pasta" data-ir="${esc(p)}">
                  ${esc(nome || p)}</button></li>`);
  }
  for (const o of d.objetos.slice(0, 30)) {
    itens.push(`<li><div class="arquivo ${o.imagem ? "" : "outro"}">
      ${esc(o.nome)}<span class="tam">${Math.round((o.bytes || 0) / 1024)} KB</span>
    </div></li>`);
  }

  $("orNavegador").innerHTML =
    (miniaturas.length
      ? `<div class="miniaturas">${miniaturas.map((o) =>
          `<img src="${esc(o.url)}" alt="" loading="lazy">`).join("")}</div>`
      : "") +
    (itens.length ? `<ul>${itens.join("")}</ul>`
                  : `<p class="aviso-nav">Pasta vazia.</p>`) +
    (d.tem_mais
      ? `<p class="aviso-nav">Mostrando os primeiros itens — a pasta tem mais.</p>`
      : "") +
    (!d.assina && d.imagens
      ? `<p class="aviso-nav">Sem miniaturas: a credencial desta instalação não
         assina URL (precisa de conta de serviço).</p>`
      : "");

  $("orNavegador").querySelectorAll("[data-ir]").forEach((b) =>
    b.addEventListener("click", () => navegar(b.dataset.ir)));
}

function desenharCaminho(d) {
  const partes = d.prefixo ? d.prefixo.replace(/\/$/, "").split("/") : [];
  let acumulado = "";
  const trilha = [`<button type="button" data-ir="">${esc(d.bucket)}</button>`];
  partes.forEach((p, i) => {
    acumulado += p + "/";
    trilha.push(`<span class="sep">/</span>`);
    trilha.push(i === partes.length - 1
      ? `<span class="atual">${esc(p || "/")}</span>`
      : `<button type="button" data-ir="${esc(acumulado)}">${esc(p || "/")}</button>`);
  });
  $("orCaminho").innerHTML = trilha.join("");
  $("orCaminho").querySelectorAll("[data-ir]").forEach((b) =>
    b.addEventListener("click", () => navegar(b.dataset.ir)));
}

$("orBucket").addEventListener("change", () => navegar(""));

$("formOrigem").addEventListener("submit", comTravaDeEnvio($("formOrigem"), async () => {
  const limite = $("orLimite").value;
  try {
    avisar("Catalogando… isso lê a listagem do bucket, sem baixar imagens.");
    const r = await post(`/projects/${estado.projeto}/imports?dry_run=false`, {
      kind: "gcs_prefix", bucket: $("orBucket").value, prefix: $("orPrefixo").value,
      limit: limite ? Number(limite) : null,
    });
    $("dlgOrigem").close();
    // Vincular ANTES de esperar. O `source_id` já veio do POST e o vínculo não
    // depende de a catalogação ter terminado; esperando primeiro, a espera que
    // estoura deixava a origem sem vínculo nenhum — o usuário via um dataset
    // vazio e nada que ligasse uma coisa à outra. E a espera passou a incluir
    // a vez na fila, porque a importação agora é executada uma de cada vez.
    await api(`/projects/${estado.projeto}/datasets/${estado.atual}` +
              `/sources/${r.source_id}`, { method: "PUT" });
    $("formOrigem").reset();
    try {
      await esperarJob(r.job_id);
    } finally {
      await abrirDataset(estado.atual);
    }
  } catch (e) { avisar(e.message, true); }
}));
