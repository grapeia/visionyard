/* O esqueleto: quem entrou, qual projeto, e a partida.
 *
 * É o módulo de entrada. Importa os outros também pelo efeito de carregá-los:
 * cada um liga os seus listeners ao ser avaliado, e um que ninguém importasse
 * viraria botão que não responde, sem erro nenhum no console. */

import "./entrada.js";
import "./dialogos.js";
import "./roi.js";
import "./bucket.js";
import "./abas.js";
import "./anotador.js";
import "./pool.js";
import "./prelabel.js";
import "./parametros.js";
import "./usuarios.js";
import { carregarDatasets } from "./datasets.js";
import { carregarModelos, modelos } from "./modelos.js";
import { $, api, avisar, erroAviso, esc, estado, mostrarEntrada, nomePapel } from "./nucleo.js";
import { carregarProjetoTab } from "./projeto.js";
import { carregarDados, dados, preencherEscopo } from "./rotulagem.js";
import { carregarRuns } from "./treino.js";

export async function iniciar() {
  let eu;
  try { eu = await api("/me"); } catch { return mostrarEntrada(); }
  estado.usuario = eu;
  $("quemSou").textContent = eu.display_name;
  // Administração da instalação: só o superusuário cadastra devices e usuários.
  $("abaBotaoDevices").hidden = !eu.is_superuser;
  $("abaBotaoUsuarios").hidden = !eu.is_superuser;
  $("abaBotaoParametros").hidden = !eu.is_superuser;
  $("telaEntrada").hidden = true;
  $("telaApp").hidden = false;

  // Antes do retorno de "nenhum projeto": numa instalação recém-criada o
  // superusuário não participa de projeto algum, e é por este botão que ele
  // cria o primeiro. Depois do retorno, a tela ficava sem saída.
  $("botaoNovoProjeto").hidden = !estado.usuario.is_superuser;
  const projetos = await api("/projects");
  const sel = $("seletorProjeto");
  if (!projetos.length) {
    sel.innerHTML = "<option>nenhum projeto</option>";
    $("painelDataset").innerHTML =
      `<div class="vazio"><p>Você ainda não participa de nenhum projeto.</p>
       <p class="dica">Peça a um administrador para incluir você.</p></div>`;
    // Projeto é a aba de entrada: sem isto ela ficava em "Carregando…".
    await carregarProjetoTab();
    return;
  }
  sel.innerHTML = projetos
    .map((p) => `<option value="${esc(p.slug)}">${esc(p.name)} · ${esc(nomePapel(p.role))}</option>`)
    .join("");
  estado.projetos = projetos;
  estado.projeto = projetos[0].slug;
  estado.papel = projetos[0].role;   // papel disponível em qualquer aba, já aqui
  // Projeto é a primeira aba e a de entrada. Ainda assim carregamos classes e
  // datasets aqui: estado.classes serve o anotador e estado.datasets serve o
  // escopo e o diálogo de treino, ambos usados por outras abas.
  await carregarProjeto();
  await carregarDatasets();
  preencherEscopo();
  await carregarProjetoTab();
}

$("seletorProjeto").addEventListener("change", async (ev) => {
  estado.projeto = ev.target.value;
  estado.papel = ((estado.projetos || []).find((p) => p.slug === estado.projeto)
                  || {}).role;
  estado.atual = null;
  dados.pagina = 0; dados.itens = []; dados.dataset = "";
  await carregarProjeto();
  try { await carregarDatasets(); } catch (e) { avisar(e.message, true); }
  preencherEscopo();
  if (!$("abaProjeto").hidden) await carregarProjetoTab().catch(erroAviso);
  if (!$("abaDados").hidden) await carregarDados();
  if (!$("abaTreino").hidden) await carregarRuns().catch(erroAviso);
  if (!$("abaModelos").hidden) { modelos.atual = null; await carregarModelos().catch(erroAviso); }
});

/** Classes e tipo do projeto: o painel de anotação precisa deles. */
export async function carregarProjeto() {
  const p = await api(`/projects/${estado.projeto}`);
  const classes = (p.schema || {}).classes || [];
  estado.classes = classes.map((c) => c.name);
  // `inside` é o índice da principal que recorta a classe (segmentação);
  // nulo é principal. Por índice, como no YOLO.
  estado.dentroDe = classes.map((c) => (c.inside ?? null));
  estado.tarefa = p.task_type;
}

// Só 401 leva ao login — e disso `api()` já cuida. Tratar qualquer erro como
// falha de autenticação criava um laço: o usuário logava, a carga quebrava no
// mesmo ponto e ele voltava para o login, sem nunca ver a causa.
iniciar().catch((e) => avisar(e.message, true));
