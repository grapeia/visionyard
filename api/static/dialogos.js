/* O que vale para todo <dialog> da tela: cancelar sem submeter, e fechar
 * sem concluir limpar o formulário. */

import { $ } from "./nucleo.js";
import { esquecerClasses } from "./projeto.js";

// Cancelar não é submit: se fosse, seria o botão padrão do formulário (é o
// primeiro) e o Enter em qualquer campo acionaria cancelar, apagando o que o
// usuário digitou. Com type=button, o Enter aciona o botão primário.
document.querySelectorAll("dialog .cancelar").forEach((b) =>
  b.addEventListener("click", () => b.closest("dialog").close()));

// Fechar sem concluir limpa o formulário: reabrir não deve trazer o rascunho
// anterior. Vale para o botão Cancelar e para o Esc.
for (const id of ["dlgDataset", "dlgOrigem", "dlgCongelar", "dlgRun", "dlgRoi",
                  "dlgDevice", "dlgProjeto", "dlgPrelabel", "dlgExport", "dlgDeploy", "dlgEvaluate", "dlgUsuario"]) {
  $(id).addEventListener("close", (ev) => {
    if (ev.target.returnValue === "ok") return;
    ev.target.querySelector("form").reset();
    delete $("dsSlug").dataset.tocado;
    delete $("devSlug").dataset.tocado;
    delete $("pjSlug").dataset.tocado;
    esquecerClasses();
  });
}
