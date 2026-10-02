/* Aba Parâmetros: configuração da instalação (superusuário).
 *
 * Campo definido no ambiente do servidor aparece travado, com o nome da
 * variável: quem destrava é quem a escreveu, não um clique aqui. A
 * credencial do GCS não se cadastra nesta tela — ela mostra qual conta a API
 * usa e o que essa conta consegue fazer nos buckets. */

import { $, api, avisar, esc } from "./nucleo.js";

const ORIGEM = {
  ambiente: ["travado no ambiente", "neutro"],
  tela: ["definido aqui", "ok"],
  "padrão": ["padrão", "espera"],
};

export async function carregarParametros() {
  let ps;
  try { ps = await api("/parameters"); }
  catch (e) { $("painelParametros").innerHTML =
    `<div class="vazio"><p>${esc(e.message)}</p></div>`; return; }
  $("painelParametros").innerHTML = `
    <ul class="lista-users">${ps.map(cartaoParametro).join("")}</ul>
    <section class="user diag">
      <div class="user-topo">
        <span class="user-nome">Acesso ao armazenamento</span>
        <span class="espaco"></span>
        <button class="discreto" id="botaoDiagnostico">testar acesso</button>
      </div>
      <p class="dica">A API acessa o GCS pela credencial do próprio servidor —
        na nuvem, a identidade da máquina; ela não é cadastrada aqui. O teste
        diz qual conta é, se ela assina URL e o que falta em cada bucket. Não
        grava nada.</p>
      <div id="resultadoDiagnostico"></div>
    </section>`;
  ligarAcoesParametro();
}

function campo(p) {
  const id = `par-${esc(p.chave)}`;
  const trava = p.origem === "ambiente" ? "disabled" : "";
  if (p.tipo === "lista") {
    return `<textarea id="${id}" rows="3" ${trava}
      placeholder="um por linha">${esc((p.valor || []).join("\n"))}</textarea>`;
  }
  const tipo = p.tipo === "inteiro" ? `type="number" step="1"` : "";
  return `<input id="${id}" ${tipo} value="${esc(p.valor ?? "")}" ${trava}>`;
}

function cartaoParametro(p) {
  const [rot, selo] = ORIGEM[p.origem] || [p.origem, "neutro"];
  const quando = p.alterado_em
    ? `alterado ${esc(new Date(p.alterado_em).toLocaleString("pt-BR"))}`
      + (p.alterado_por ? ` por ${esc(p.alterado_por)}` : "")
    : "";
  const travado = p.origem === "ambiente";
  return `<li class="user">
    <div class="user-topo">
      <span class="user-nome">${esc(p.rotulo)}</span>
      <span class="selo-run ${selo}">${esc(rot)}</span>
      <span class="espaco"></span>
      <span class="dica">${quando}</span>
    </div>
    <p class="dica">${esc(p.ajuda)}</p>
    ${campo(p)}
    ${travado
      ? `<p class="dica">Definido por <code class="mono">${esc(p.variavel)}</code>
         no servidor. Para editar aqui, retire a variável e reinicie a API.</p>`
      : `<div class="user-acoes">
          <button class="discreto" data-salvar="${esc(p.chave)}"
            data-tipo="${esc(p.tipo)}">salvar</button>
          ${p.origem === "tela"
            ? `<button class="discreto" data-restaurar="${esc(p.chave)}">
                voltar ao padrão</button>` : ""}
        </div>`}
  </li>`;
}

function lerCampo(chave, tipo) {
  const bruto = $(`par-${chave}`).value;
  if (tipo === "lista") return bruto.split("\n").map((s) => s.trim()).filter(Boolean);
  if (tipo === "inteiro") return bruto.trim() === "" ? null : Number(bruto);
  return bruto;
}

function ligarAcoesParametro() {
  const painel = $("painelParametros");
  painel.querySelectorAll("[data-salvar]").forEach((b) =>
    b.addEventListener("click", () => salvar(b, b.dataset.salvar, b.dataset.tipo)));
  painel.querySelectorAll("[data-restaurar]").forEach((b) =>
    b.addEventListener("click", () => restaurar(b, b.dataset.restaurar)));
  $("botaoDiagnostico").addEventListener("click", diagnosticar);
}

async function salvar(botao, chave, tipo) {
  botao.disabled = true;
  try {
    await api(`/parameters/${encodeURIComponent(chave)}`, {
      method: "PUT", body: JSON.stringify({ valor: lerCampo(chave, tipo) }) });
    avisar("parâmetro salvo");
    await carregarParametros();
  } catch (e) { avisar(e.message, true); botao.disabled = false; }
}

async function restaurar(botao, chave) {
  if (!confirm("Voltar este parâmetro ao padrão do servidor?")) return;
  botao.disabled = true;
  try {
    await api(`/parameters/${encodeURIComponent(chave)}`, { method: "DELETE" });
    avisar("parâmetro voltou ao padrão");
    await carregarParametros();
  } catch (e) { avisar(e.message, true); botao.disabled = false; }
}

const PERMISSAO = {
  "storage.objects.list": "listar", "storage.objects.get": "ler",
  "storage.objects.create": "gravar",
};

function linhaBucket(b) {
  const estado = b.erro
    ? `<span class="selo-run ruim">erro</span> <span class="dica ruim">${esc(b.erro)}</span>`
    : b.faltando.length
      ? `<span class="selo-run ruim">sem ${esc(b.faltando.map((p) => PERMISSAO[p] || p).join(", "))}</span>`
      : `<span class="selo-run ok">ok</span>`;
  return `<li><span class="mono">${esc(b.bucket)}</span>
    <span class="dica">(${esc(b.papel)})</span> ${estado}</li>`;
}

async function diagnosticar() {
  const alvo = $("resultadoDiagnostico");
  alvo.innerHTML = `<p class="carregando">Testando…</p>`;
  let d;
  try { d = await api("/parameters/diagnostico"); }
  catch (e) { alvo.innerHTML = `<p class="dica ruim">${esc(e.message)}</p>`; return; }
  const assina = d.assina
    ? `<span class="selo-run ok">assina URL</span>`
    : `<span class="selo-run ruim">não assina URL</span>
       <span class="dica ruim">sem isso a galeria fica sem miniatura e as
       máquinas não baixam nada — falta conta de serviço, ou a permissão
       iam.serviceAccounts.signBlob na conta da máquina</span>`;
  alvo.innerHTML = `
    <p>Conta: <span class="mono">${esc(d.conta || "nenhuma")}</span>
      <span class="dica">${esc(d.origem || "")}</span></p>
    <p>${assina}</p>
    ${d.erro ? `<p class="dica ruim">${esc(d.erro)}</p>` : ""}
    ${d.buckets.length
      ? `<ul class="diag-buckets">${d.buckets.map(linhaBucket).join("")}</ul>`
      : `<p class="dica">Nenhum bucket configurado.</p>`}`;
}
