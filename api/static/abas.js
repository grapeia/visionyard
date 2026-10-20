/* A barra de abas: uma porta só para trocar de tela, e a ajuda de cada uma.
 *
 * `mostrarAba` é o cubo do grafo — conhece o carregador de cada aba. Fica
 * fora do módulo de entrada para que quem precisa dela (o anotador, a aba
 * Datasets) não tenha de importar o esqueleto inteiro. */

import { carregarDatasets } from "./datasets.js";
import { carregarModelos } from "./modelos.js";
import { $, erroAviso } from "./nucleo.js";
import { carregarDevices } from "./pool.js";
import { carregarProjetoTab } from "./projeto.js";
import { carregarDados } from "./rotulagem.js";
import { carregarRuns, pararPollRun } from "./treino.js";
import { carregarParametros } from "./parametros.js";
import { carregarUsuarios } from "./usuarios.js";

$("abas").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-aba]");
  if (b) mostrarAba(b.dataset.aba);
});

// ---- ajuda por tela (botão ? na barra de abas) ----
// Glossário em linguagem simples: o que cada termo da tela significa.
const AJUDA = {
  projeto: ["Projeto", `
    <dl>
      <dt>Projeto</dt><dd>O espaço de trabalho de um cliente/linha (imagens,
        datasets, modelos e máquinas ficam dentro dele).</dd>
      <dt>Papéis</dt><dd><b>Dono</b> e <b>Administrador</b> gerenciam tudo;
        <b>Engenheiro</b> treina, compila e implanta modelos; <b>Anotador</b> só
        rotula imagens; <b>Visualizador</b> só consulta.</dd>
    </dl>`],
  datasets: ["Datasets", `
    <dl>
      <dt>Dataset</dt><dd>Um conjunto de imagens escolhidas para ensinar ou
        avaliar o modelo.</dd>
      <dt>Versão congelada</dt><dd>Uma "foto" do dataset num momento: as imagens
        e rótulos daquele instante, imutáveis. Treino e avaliação sempre usam uma
        versão congelada, para o resultado ser reprodutível.</dd>
      <dt>ROI</dt><dd>Região de interesse: o recorte da imagem que o modelo vê.
        O que fica fora é ignorado no treino e na inferência.</dd>
      <dt>Benchmark</dt><dd>Versão congelada usada como prova: os modelos são
        medidos contra ela para comparar quem acerta mais.</dd>
    </dl>`],
  dados: ["Rotulagem", `
    <dl>
      <dt>Rotular</dt><dd>Desenhar as caixas e dizer a classe de cada objeto na
        imagem — é disso que o modelo aprende.</dd>
      <dt>Pré-taguear</dt><dd>Um modelo já treinado passa nas imagens e deixa
        caixas de rascunho para você só corrigir, em vez de desenhar do zero.
        Nunca sobrescreve o que você já rotulou.</dd>
      <dt>Sem minha anotação</dt><dd>Imagens que você ainda não rotulou (outra
        pessoa pode ter rotulado).</dd>
    </dl>`],
  treino: ["Treinamento", `
    <dl>
      <dt>Treino (run)</dt><dd>Uma rodada de aprendizado: o modelo vê as imagens
        de uma versão congelada várias vezes e ajusta os pesos.</dd>
      <dt>Época</dt><dd>Uma passada completa por todas as imagens do dataset.
        Um treino tem dezenas ou centenas de épocas.</dd>
      <dt>mAP</dt><dd>Nota de 0 a 1 de quanto o modelo acerta (caixas certas,
        classes certas). Quanto maior, melhor.</dd>
      <dt>val_loss</dt><dd>Erro medido em imagens que o modelo não viu no
        treino. Deve cair ao longo das épocas; se sobe, o modelo decorou.</dd>
      <dt>Máquina-alvo</dt><dd>Onde o treino roda: uma máquina específica ou
        qualquer uma do pool com capacidade <code>train</code>.</dd>
    </dl>`],
  modelos: ["Modelos", `
    <dl>
      <dt>Versão</dt><dd>Um modelo pronto e congelado (pesos + métricas de um
        treino). Nunca muda — se retreinar, nasce outra versão.</dd>
      <dt>Champion</dt><dd>A versão que está valendo em produção. "Promover a
        champion" = colocar essa versão para trabalhar na linha.</dd>
      <dt>Challenger</dt><dd>A candidata: uma versão nova que está sendo avaliada
        para tentar tomar o lugar do champion.</dd>
      <dt>Baseline</dt><dd>A referência a bater — normalmente a primeira versão
        aceitável. Serve para saber se o projeto está evoluindo.</dd>
      <dt>Compilar (build)</dt><dd>Converter o modelo treinado (.pt) para o
        formato do dispositivo. <b>Engine TensorRT</b> é o formato rápido das
        máquinas NVIDIA/Jetson (~7× mais rápido); <b>ONNX</b> é o portátil.</dd>
      <dt>FP16 / INT8</dt><dd>Precisão dos números dentro do engine. FP16 é o
        seguro (mesmo resultado do .pt); INT8 é mais rápido, mas aproxima mais
        (precisa calibrar e pode divergir um pouco).</dd>
      <dt>Shapes dinâmicos</dt><dd>Deixa o engine aceitar o mesmo enquadramento
        do .pt. Com FP16 + dinâmico o engine detecta idêntico ao .pt — é a
        combinação recomendada para Jetson.</dd>
      <dt>DLA</dt><dd>Acelerador extra dos Jetson (poupa a GPU). Exige shape
        fixo e INT8/FP16.</dd>
      <dt>Avaliar</dt><dd>Medir uma versão (ou um build dela) contra um
        benchmark: mAP + tempo de inferência na máquina-alvo.</dd>
      <dt>Implantar</dt><dd>Publicar o build escolhido para o dispositivo da
        linha baixar e usar.</dd>
    </dl>`],
  devices: ["Pool", `
    <dl>
      <dt>Pool</dt><dd>O conjunto de máquinas disponíveis para executar
        trabalhos. Um trabalho sem máquina fixada vai para qualquer uma do
        pool que cumpra os requisitos.</dd>
      <dt>Máquina (device)</dt><dd>Um computador registrado no sistema (Jetson,
        servidor com GPU…) que executa trabalhos.</dd>
      <dt>Capacidades</dt><dd><b>train</b> treina; <b>infer</b> compila engines,
        avalia e roda modelos; <b>prelabel</b> pré-tagueia imagens.</dd>
      <dt>Agente (serviço local)</dt><dd>O programa que roda na própria máquina
        e conversa com o sistema: se apresenta com o token, puxa trabalhos da
        fila e reporta progresso. Cadastrar a máquina aqui não basta — sem o
        agente rodando nela, fica "fora do ar" e não executa nada.</dd>
      <dt>Token</dt><dd>A senha da máquina: o agente usa para se apresentar à
        API e pegar trabalhos da fila.</dd>
      <dt>Tasks</dt><dd>A fila de trabalho: o que aguarda máquina e o que está
        em execução (treino, compilação, avaliação, pré-tag). "pool" = qualquer
        máquina compatível pode pegar; "(fixado)" = amarrado a uma máquina
        específica. Cancelar na fila é imediato; rodando, a máquina para no
        próximo heartbeat.</dd>
    </dl>`],
  usuarios: ["Usuários", `
    <dl>
      <dt>Papéis</dt><dd><b>Administrador</b> gerencia projetos e pessoas;
        <b>Engenheiro</b> treina/compila/implanta; <b>Anotador</b> rotula.</dd>
      <dt>Membro do projeto</dt><dd>O papel vale por projeto: a mesma pessoa
        pode ser Engenheiro num projeto e Anotador em outro.</dd>
    </dl>`],
  parametros: ["Parâmetros", `
    <dl>
      <dt>Origem</dt><dd><b>travado no ambiente</b>: o servidor define por
        variável, e ela vence esta tela; <b>definido aqui</b>: gravado nesta
        tela; <b>padrão</b>: ninguém definiu.</dd>
      <dt>Quando vale</dt><dd>Na hora para a API; para o executor de
        importação rodando à parte, em até 10 segundos.</dd>
      <dt>Credencial</dt><dd>Não se cadastra aqui: a API usa a do servidor.
        "Testar acesso" mostra qual conta é e o que ela consegue fazer.</dd>
    </dl>`],
};

$("botaoAjuda").addEventListener("click", () => {
  const atual = document.querySelector('#abas [data-aba][aria-current="true"]');
  const [titulo, corpo] = AJUDA[atual ? atual.dataset.aba : "projeto"]
    || ["Ajuda", "<p>Sem ajuda para esta tela ainda.</p>"];
  $("ajudaTitulo").textContent = titulo;
  $("ajudaCorpo").innerHTML = corpo;
  $("dlgAjuda").showModal();
});

/** Uma única porta para trocar de aba: cuida do aria-current, do que fica
 *  visível e do que precisa carregar ao entrar. Antes só havia duas abas e a
 *  lógica era um if binário — não escala para Treinamento e Máquinas. */
export function mostrarAba(nome) {
  document.querySelectorAll("#abas [data-aba]").forEach((x) =>
    x.setAttribute("aria-current", x.dataset.aba === nome));
  for (const [aba, el] of Object.entries({
    projeto: "abaProjeto", datasets: "abaDatasets", dados: "abaDados",
    treino: "abaTreino", modelos: "abaModelos", devices: "abaDevices",
    usuarios: "abaUsuarios", parametros: "abaParametros",
  })) $(el).hidden = aba !== nome;
  // Sair da aba de treino encerra o polling ao vivo — senão ele segue batendo
  // na API em segundo plano por uma tela que ninguém está vendo.
  if (nome !== "treino") pararPollRun();
  // Toda aba relê o que mostra ao entrar: o que mudou noutra aba (versão
  // congelada, treino que terminou, imagens importadas) ou noutra sessão só
  // aparecia recarregando a página. A Rotulagem relê a página em que estava.
  if (nome === "projeto") carregarProjetoTab().catch(erroAviso);
  if (nome === "datasets") carregarDatasets().catch(erroAviso);
  if (nome === "dados") carregarDados().catch(erroAviso);
  if (nome === "treino") carregarRuns().catch(erroAviso);
  if (nome === "modelos") carregarModelos().catch(erroAviso);
  if (nome === "devices") carregarDevices().catch(erroAviso);
  if (nome === "usuarios") carregarUsuarios().catch(erroAviso);
  if (nome === "parametros") carregarParametros().catch(erroAviso);
}
