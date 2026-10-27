/* Administração das permissões da instalação, exclusiva do superusuário. */
import { $, api, avisar, esc, estado } from "./nucleo.js";

let matriz = null;
let enviando = false;
const ORDEM = ["viewer", "annotator", "engineer", "admin", "owner"];


export async function carregarPermissoes() {
  if (!estado.usuario?.is_superuser) return;
  $("painelPermissoes").innerHTML = '<p class="carregando">Carregando…</p>';
  try {
    matriz = await api("/permissions");
    render();
  } catch (e) {
    $("painelPermissoes").innerHTML = `<p role="alert">${esc(e.message)}</p>
      <button id="relerPermissoes">Tentar novamente</button>`;
    $("relerPermissoes").onclick = carregarPermissoes;
  }
}

function render() {
  let anterior = "";
  const linhas = matriz.acoes.map((acao) => {
    const grupo = [acao.pagina, acao.secao].join(" → ");
    const cabeca = grupo !== anterior
      ? `<tr class="permissoes-grupo"><th colspan="${matriz.papeis.length + 2}" scope="rowgroup">${esc(grupo)}</th></tr>` : "";
    anterior = grupo;
    return cabeca + `<tr><th scope="row">${esc(acao.nome)}
      <small>${esc(acao.ajuda)}</small><code>${esc(acao.chave)}</code>
      ${acao.sensivel ? '<small class="permissao-alerta">Sensível: permite administrar membros e conceder acesso ao projeto.</small>' : ""}
      </th>${matriz.papeis.map((papel) => {
        const valor = acao.papeis[papel.chave];
        return `<td class="${valor.alterado ? "permissao-alterada" : ""}">
          <label><input type="checkbox" data-acao="${esc(acao.chave)}"
            data-papel="${esc(papel.chave)}" ${valor.permitido ? "checked" : ""}
            ${enviando ? "disabled" : ""} aria-label="${esc(acao.nome)} — ${esc(papel.nome)}">
          <span class="sr-only">${esc(papel.nome)}</span></label>
          ${valor.alterado ? `<small>Personalizado</small><button class="discreto"
            data-restaurar="${esc(acao.chave)}" data-papel="${esc(papel.chave)}"
            ${enviando ? "disabled" : ""} aria-label="Restaurar ${esc(acao.nome)} para ${esc(papel.nome)}">Restaurar</button>` : ""}
          </td>`;
      }).join("")}<td><input type="checkbox" checked disabled aria-label="Dono: sempre permitido"></td></tr>`;
  }).join("");
  $("painelPermissoes").innerHTML = `<p class="dica">Página → seção → ação. Cada marcação vale para aquele papel em todos os projetos.
    Donos sempre têm todas as permissões. Restaurar retorna ao padrão da ação.</p>
    <div class="permissoes-tabela"><table class="tab-versoes"><thead><tr><th>Ação</th>
      ${matriz.papeis.map((p) => `<th scope="col">${esc(p.nome)}</th>`).join("")}
      <th scope="col">Dono (fixo)</th></tr></thead><tbody>${linhas}</tbody></table></div>`;
}

async function alterar(chave, papel, permitido, restaurar = false) {
  if (enviando || !estado.usuario?.is_superuser) return;
  const acao = matriz.acoes.find((a) => a.chave === chave);
  const concede = restaurar ? ORDEM.indexOf(papel) >= ORDEM.indexOf(acao.padrao) : permitido;
  if (acao.sensivel && concede && ORDEM.indexOf(papel) < ORDEM.indexOf(acao.padrao)
      && !confirm("Esta permissão permite gerenciar membros e conceder acesso ao projeto até o próprio papel. Conceder a um papel abaixo do padrão?")) {
    render(); return;
  }
  enviando = true; render();
  try {
    const atualizada = await api(`/permissions/${encodeURIComponent(chave)}/${encodeURIComponent(papel)}`,
      restaurar ? { method: "DELETE" } : { method: "PUT", body: JSON.stringify({ permitido }) });
    matriz.acoes = matriz.acoes.map((a) => a.chave === chave ? atualizada : a);
    avisar(restaurar ? "Permissão restaurada." : "Permissão atualizada.");
  } catch (e) { avisar(e.message, true); }
  finally { enviando = false; render(); }
}

$("painelPermissoes").addEventListener("change", (ev) => {
  const b = ev.target.closest("[data-acao]");
  if (b) alterar(b.dataset.acao, b.dataset.papel, b.checked);
});
$("painelPermissoes").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-restaurar]");
  if (b) alterar(b.dataset.restaurar, b.dataset.papel, false, true);
});
