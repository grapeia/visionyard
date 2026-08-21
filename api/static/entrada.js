/* Login e primeiro acesso por convite. */

import { iniciar } from "./app.js";
import { $, api, mostrarEntrada, post } from "./nucleo.js";

$("formEntrada").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const erro = $("erroEntrada");
  erro.hidden = true;
  try {
    await post("/login", { email: $("email").value, password: $("senha").value });
    $("senha").value = "";
    await iniciar();
  } catch (e) {
    erro.textContent = e.message;
    erro.hidden = false;
  }
});

$("botaoSair").addEventListener("click", async () => {
  await api("/logout", { method: "POST" }).catch(() => {});
  mostrarEntrada();
});

// Primeiro acesso: aceitar convite (define a senha e já entra).
$("trocaConvite").addEventListener("click", () => {
  const convite = $("formConvite").hidden;
  $("formEntrada").hidden = convite;
  $("formConvite").hidden = !convite;
  $("trocaConvite").textContent = convite ? "Já tenho senha" : "Tenho um convite";
});

$("formConvite").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const erro = $("erroConvite");
  erro.hidden = true;
  try {
    await post("/invite/accept", { token: $("convToken").value.trim(),
                                   password: $("convSenha").value });
    $("convSenha").value = "";
    await iniciar();
  } catch (e) {
    erro.textContent = e.message;
    erro.hidden = false;
  }
});
