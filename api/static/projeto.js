/* Aba Projeto: vocabulário de classes, membros e criação de projeto.
 *
 * Como o schema é imutável, acrescentar classe gera uma versão nova
 * back-compatible — os índices antigos não mudam, então nada do que já foi
 * rotulado é invalidado. */

import { carregarProjeto } from "./app.js";
import { carregarDatasets } from "./datasets.js";
import { $, RANK, api, avisar, comTravaDeEnvio, cor, esc, estado, num, post } from "./nucleo.js";
import { dados, preencherEscopo } from "./rotulagem.js";

export const tipoProjeto = (t) =>
  ({ detect: "Detecção", segment: "Segmentação", classify: "Classificação" }[t] || t);

export async function carregarProjetoTab() {
  if (!estado.projeto) {
    $("painelProjetoTab").innerHTML = `<div class="vazio">
      <p>Você ainda não participa de nenhum projeto.</p></div>`;
    return;
  }
  const d = await api(`/projects/${estado.projeto}`);
  estado.papel = d.role;
  const podeEditar = RANK[d.role] >= RANK.engineer;
  const classes = (d.schema && d.schema.classes) || [];
  const c = d.counts || {};

  $("painelProjetoTab").innerHTML = `
    <div class="cabeca">
      <h1>${esc(d.name)}</h1>
      <span class="tipo-ds">${esc(tipoProjeto(d.task_type))}</span>
      <span class="mono slug">${esc(d.slug)}</span>
    </div>
    <div class="run-meta">
      <span>${num(c.samples)} imagens</span>
      <span>${num(c.annotations)} anotações</span>
      <span>${num(c.datasets)} datasets</span>
      <span>${num(c.models)} modelos</span>
      <span>seu papel: ${esc(d.role || "—")}</span>
    </div>

    <section class="secao">
      <header><span class="rotulo">Classes — schema v${d.schema ? d.schema.version : 1}</span></header>
      <ol class="classes-schema">
        ${classes.map((cl) => `<li>
          <span class="idx">${cl.id}</span>
          <span class="cor" style="background:${cor(cl.id)}"></span>
          <span class="nome">${esc(cl.name)}</span></li>`).join("")}
      </ol>
      ${podeEditar ? `
        <form class="add-classe" id="formAddClasse">
          <input id="addClasseNome" placeholder="nova classe"
                 pattern="[a-z0-9_]+" title="minúsculas, números e _" required>
          <button class="primario" type="submit">Adicionar classe</button>
        </form>
        <p class="dica">Entra como índice ${classes.length}, gera o schema
          v${(d.schema ? d.schema.version : 1) + 1} e não invalida o que já foi
          rotulado. Renomear ou reordenar é outra história (quebra compatibilidade)
          e ainda não é feito por aqui.</p>`
        : `<p class="dica">Só engenheiro ou acima altera o vocabulário.</p>`}
    </section>

    <section class="secao">
      <header><span class="rotulo">Histórico de schema</span></header>
      <ul class="trilho">
        ${(d.schemas || []).map((s) => `<li class="versao">
          <div class="versao-topo">
            <span class="versao-n">v${s.version}</span>
            <span class="selo-run ${s.back_compatible ? "ok" : "ruim"}">
              ${s.back_compatible ? "compatível" : "quebra compat"}</span>
          </div>
          <p class="versao-desc">${esc(s.change_note || "")} · ${s.n_classes} classes
            · ${new Date(s.created_at).toLocaleDateString("pt-BR")}</p>
        </li>`).join("")}
      </ul>
    </section>`;

  const f = $("formAddClasse");
  if (f) f.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const nome = $("addClasseNome").value.trim();
    if (!nome) return;
    const botao = f.querySelector("button");
    botao.disabled = true;
    try {
      const r = await post(`/projects/${estado.projeto}/schema`, { add: [nome] });
      avisar(`classe "${nome}" adicionada · schema v${r.version}`);
      await carregarProjeto();          // atualiza estado.classes p/ o anotador
      await carregarProjetoTab();
    } catch (e) { avisar(e.message, true); }
    finally { botao.disabled = false; }
  });
  renderMembros(d.slug, RANK[d.role] >= RANK.admin).catch(() => {});
}

const PAPEIS = ["viewer", "annotator", "engineer", "admin", "owner"];

/** Membros do projeto: quem tem acesso e com que papel. Admin+ concede/remove;
 *  owner só um owner concede (o backend garante). */
async function renderMembros(slug, podeAdmin) {
  const membros = await api(`/projects/${slug}/members`).catch(() => []);
  const sec = document.createElement("section");
  sec.className = "secao";
  sec.innerHTML = `<header><span class="rotulo">Membros</span></header>
    <ul class="lista-membros">${membros.map((m) => `
      <li><span class="user-nome">${esc(m.display_name)}</span>
        <span class="mono slug">${esc(m.email)}</span>
        <span class="espaco"></span>
        ${podeAdmin ? `<select class="papel-sel" data-membro="${esc(m.email)}">
          ${PAPEIS.map((p) => `<option value="${p}" ${p === m.role ? "selected" : ""}>${p}</option>`).join("")}
        </select>
        <button class="discreto ruim" data-remover-membro="${esc(m.id)}">remover</button>`
        : `<span class="selo-run neutro">${esc(m.role)}</span>`}</li>`).join("")}</ul>
    ${podeAdmin ? `<form class="add-membro" id="formMembro">
      <input id="mbEmail" type="email" placeholder="email de um usuário" required>
      <select id="mbPapel">${PAPEIS.map((p) =>
        `<option value="${p}" ${p === "annotator" ? "selected" : ""}>${p}</option>`).join("")}</select>
      <button class="primario" type="submit">Adicionar</button>
    </form>
    <p class="dica">O usuário precisa existir (aba Usuários). "owner" só um owner concede.</p>` : ""}`;
  $("painelProjetoTab").appendChild(sec);
  if (!podeAdmin) return;

  sec.querySelectorAll(".papel-sel").forEach((s) =>
    s.addEventListener("change", () => setMembro(slug, s.dataset.membro, s.value)));
  sec.querySelectorAll("[data-remover-membro]").forEach((b) =>
    b.addEventListener("click", () => removerMembro(slug, b.dataset.removerMembro)));
  $("formMembro").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    await setMembro(slug, $("mbEmail").value, $("mbPapel").value);
  });
}

async function setMembro(slug, email, role) {
  try {
    await api(`/projects/${slug}/members`,
              { method: "PUT", body: JSON.stringify({ email, role }) });
    avisar(`${email}: ${role}`);
    await carregarProjetoTab();
  } catch (e) { avisar(e.message, true); }
}

async function removerMembro(slug, userId) {
  if (!confirm("Remover este membro do projeto?")) return;
  try {
    await api(`/projects/${slug}/members/${userId}`, { method: "DELETE" });
    await carregarProjetoTab();
  } catch (e) { avisar(e.message, true); }
}

// ---- novo projeto (com construtor de classes em chips) ----

let pjClasses = [];

/** Esquece o rascunho de classes do diálogo de novo projeto.
 *
 * Existe porque o fechamento genérico de diálogo precisa limpá-lo, e módulo ES
 * não deixa um módulo atribuir à variável de outro — só chamá-la. */
export function esquecerClasses() { pjClasses = []; }

function renderChips() {
  $("pjChips").innerHTML = pjClasses.length
    ? pjClasses.map((c, i) => `<span class="chip">
        <span class="chip-idx">${i}</span>${esc(c)}
        <button type="button" data-rm="${i}" aria-label="remover">×</button>
      </span>`).join("")
    : `<span class="chip-vazio">nenhuma classe ainda</span>`;
  $("pjChips").querySelectorAll("[data-rm]").forEach((b) =>
    b.addEventListener("click", () => {
      pjClasses.splice(Number(b.dataset.rm), 1); renderChips();
    }));
}

function addChip() {
  const inp = $("pjClasseInput");
  const nome = inp.value.trim().toLowerCase();
  if (!nome) return;
  if (!/^[a-z0-9_]+$/.test(nome))
    return avisar("classe: só minúsculas, números e _", true);
  if (pjClasses.includes(nome)) return avisar("classe já na lista", true);
  pjClasses.push(nome); inp.value = ""; inp.focus(); renderChips();
}

$("botaoNovoProjeto").addEventListener("click", () => {
  pjClasses = []; renderChips();
  $("dlgProjeto").showModal();
});
$("pjAddClasse").addEventListener("click", addChip);
$("pjClasseInput").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") { ev.preventDefault(); addChip(); }
});
$("pjNome").addEventListener("input", (ev) => {
  const campo = $("pjSlug");
  if (campo.dataset.tocado) return;
  campo.value = ev.target.value.toLowerCase().normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "").replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
});
$("pjSlug").addEventListener("input", (ev) => { ev.target.dataset.tocado = "1"; });

$("formProjeto").addEventListener("submit", comTravaDeEnvio($("formProjeto"), async () => {
  // descarrega o que ficou digitado no campo de classe (sem "adicionar"),
  // senão a classe seria silenciosamente perdida no envio
  if ($("pjClasseInput").value.trim()) addChip();
  if (!pjClasses.length) return avisar("adicione ao menos uma classe", true);
  const slug = $("pjSlug").value;      // capturar antes do reset, senão vem vazio
  try {
    await post("/projects", {
      slug, name: $("pjNome").value,
      task_type: $("pjTipo").value, classes: pjClasses,
    });
    $("dlgProjeto").close();
    $("formProjeto").reset();
    delete $("pjSlug").dataset.tocado;
    esquecerClasses();
    avisar("Projeto criado.");
    await trocarPara(slug);
  } catch (e) { avisar(e.message, true); }
}));

/** Recarrega a lista de projetos, seleciona `slug` e atualiza o que a tela
 *  precisa. Usado após criar um projeto novo. */
async function trocarPara(slug) {
  const projetos = await api("/projects");
  const sel = $("seletorProjeto");
  sel.innerHTML = projetos
    .map((p) => `<option value="${esc(p.slug)}">${esc(p.name)} · ${esc(p.role)}</option>`)
    .join("");
  if (projetos.some((p) => p.slug === slug)) sel.value = slug;
  estado.projetos = projetos;
  estado.projeto = sel.value;
  estado.papel = ((projetos.find((p) => p.slug === estado.projeto)) || {}).role;
  estado.atual = null;
  dados.pagina = 0; dados.itens = []; dados.dataset = "";
  await carregarProjeto();
  await carregarDatasets();
  preencherEscopo();
  await carregarProjetoTab();
}
