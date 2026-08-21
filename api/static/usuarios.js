/* Aba Usuários: administração da instalação (superusuário).
 *
 * Sem serviço de e-mail, o convite e a redefinição saem como token para
 * repassar por canal seguro. */

import { $, api, avisar, comTravaDeEnvio, esc, estado, post } from "./nucleo.js";

const US_ESTADO = { active: "ativo", invited: "convidado", disabled: "desativado" };

export async function carregarUsuarios() {
  let us;
  try { us = await api("/users"); }
  catch (e) { $("painelUsuarios").innerHTML =
    `<div class="vazio"><p>${esc(e.message)}</p></div>`; return; }
  $("painelUsuarios").innerHTML = `<ul class="lista-users">${us.map(cartaoUsuario).join("")}</ul>`;
  ligarAcoesUsuario();
}

function cartaoUsuario(u) {
  const eu = u.id === (estado.usuario || {}).id;
  const selo = u.status === "active" ? "ok" : u.status === "disabled" ? "ruim" : "neutro";
  const hb = u.last_login_at
    ? new Date(u.last_login_at).toLocaleString("pt-BR") : "nunca entrou";
  return `<li class="user">
    <div class="user-topo">
      <span class="user-nome">${esc(u.display_name)}</span>
      <span class="mono slug">${esc(u.email)}</span>
      ${u.is_superuser ? `<span class="selo-alias champion">superusuário</span>` : ""}
      <span class="selo-run ${selo}">${esc(US_ESTADO[u.status] || u.status)}</span>
      ${eu ? `<span class="dica">(você)</span>` : ""}
      <span class="espaco"></span>
      <span class="dica">último acesso ${esc(hb)}</span>
    </div>
    <div class="user-acoes">
      <button class="discreto" data-convite="${esc(u.id)}"
        data-prop="${u.status === "invited" ? "invite" : "password_reset"}">
        ${u.status === "invited" ? "reenviar convite" : "redefinir senha"}</button>
      ${eu ? "" : (u.status === "disabled"
        ? `<button class="discreto" data-status="${esc(u.id)}" data-para="active">reativar</button>`
        : `<button class="discreto ruim" data-status="${esc(u.id)}" data-para="disabled">desativar</button>`)}
    </div>
  </li>`;
}

function ligarAcoesUsuario() {
  $("painelUsuarios").querySelectorAll("[data-convite]").forEach((b) =>
    b.addEventListener("click", () => emitirConvite(b.dataset.convite, b.dataset.prop)));
  $("painelUsuarios").querySelectorAll("[data-status]").forEach((b) =>
    b.addEventListener("click", () => mudarStatusUsuario(b.dataset.status, b.dataset.para)));
}

function mostrarTokenConvite(email, purpose, r) {
  const box = document.createElement("div");
  box.className = "token-box";
  const acao = purpose === "password_reset" ? "redefinir a senha" : "definir a senha";
  box.innerHTML = `<b>${esc(email)}</b> — token para ${acao}
    (vale ${r.expires_in_hours}h, uso único; repasse por canal seguro):
    <code class="mono">${esc(r.token)}</code>
    <button class="discreto" id="copiarConvite">copiar</button>`;
  $("painelUsuarios").prepend(box);
  $("copiarConvite").addEventListener("click", () =>
    navigator.clipboard.writeText(r.token).then(() => avisar("Token copiado.")));
}

async function emitirConvite(userId, purpose) {
  try {
    const r = await post(`/users/${userId}/invite?purpose=${purpose}`, {});
    mostrarTokenConvite(r.email, purpose, r);
  } catch (e) { avisar(e.message, true); }
}

async function mudarStatusUsuario(userId, para) {
  if (para === "disabled" &&
      !confirm("Desativar este usuário? As sessões dele são cortadas na hora."))
    return;
  try {
    await post(`/users/${userId}/status?status_=${para}`, {});
    await carregarUsuarios();
  } catch (e) { avisar(e.message, true); }
}

$("botaoNovoUsuario").addEventListener("click", () => $("dlgUsuario").showModal());

$("formUsuario").addEventListener("submit", comTravaDeEnvio($("formUsuario"), async () => {
  const senha = $("usSenha").value;
  const corpo = { email: $("usEmail").value, display_name: $("usNome").value,
                  is_superuser: $("usSuper").checked };
  if (senha) corpo.password = senha;
  try {
    const u = await post("/users", corpo);
    $("dlgUsuario").close();
    $("formUsuario").reset();
    await carregarUsuarios();
    // sem senha → nasce convidado: emite o convite e mostra o token
    if (!senha) {
      const r = await post(`/users/${u.id}/invite?purpose=invite`, {});
      mostrarTokenConvite(u.email, "invite", r);
    } else {
      avisar("Usuário criado.");
    }
  } catch (e) { avisar(e.message, true); }
}));
