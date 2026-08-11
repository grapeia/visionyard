"use strict";
/* visionyard — front da aba Datasets. Vanilla, sem build.
 * Revisitar a escolha quando chegar o canvas de anotação. */

const $ = (id) => document.getElementById(id);

const estado = { usuario: null, projeto: null, datasets: [], atual: null };

// ------------------------------------------------------------------ rede --

async function api(caminho, opcoes = {}) {
  const r = await fetch("/v1" + caminho, {
    credentials: "same-origin",
    headers: opcoes.body ? { "Content-Type": "application/json" } : {},
    ...opcoes,
  });
  if (r.status === 401) { mostrarEntrada(); throw new Error("sessão expirada"); }
  if (r.status === 204) return null;
  const corpo = await r.json().catch(() => null);
  if (!r.ok) throw new Error(detalhe(corpo) || `erro ${r.status}`);
  return corpo;
}

/** FastAPI devolve `detail` como texto ou como lista de erros de validação. */
function detalhe(corpo) {
  const d = corpo && corpo.detail;
  if (!d) return null;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((e) => e.msg || "").filter(Boolean).join("; ");
  return null;
}

const post = (c, dados) => api(c, { method: "POST", body: JSON.stringify(dados) });

function avisar(texto, ruim = false) {
  const el = $("aviso");
  el.textContent = texto;
  el.classList.toggle("ruim", ruim);
  el.classList.add("mostra");
  clearTimeout(avisar._t);
  avisar._t = setTimeout(() => el.classList.remove("mostra"), 3800);
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const num = (n) => Number(n ?? 0).toLocaleString("pt-BR");

// --------------------------------------------------------------- entrada --

function mostrarEntrada() {
  // Um <dialog> aberto por showModal() deixa o resto do documento inerte —
  // inclusive a tela de login que estamos prestes a mostrar. Sem fechar, a
  // sessão expirando durante um modal trava a página até dar F5.
  document.querySelectorAll("dialog[open]").forEach((d) => d.close());
  $("telaApp").hidden = true;
  $("telaEntrada").hidden = false;
  $("email").focus();
}

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

// --------------------------------------------------------------- projeto --

async function iniciar() {
  let eu;
  try { eu = await api("/me"); } catch { return mostrarEntrada(); }
  estado.usuario = eu;
  $("quemSou").textContent = eu.display_name;
  // Administração da instalação: só o superusuário cadastra devices e usuários.
  $("abaBotaoDevices").hidden = !eu.is_superuser;
  $("abaBotaoUsuarios").hidden = !eu.is_superuser;
  $("telaEntrada").hidden = true;
  $("telaApp").hidden = false;

  const projetos = await api("/projects");
  const sel = $("seletorProjeto");
  if (!projetos.length) {
    sel.innerHTML = "<option>nenhum projeto</option>";
    $("painelDataset").innerHTML =
      `<div class="vazio"><p>Você ainda não participa de nenhum projeto.</p>
       <p class="dica">Peça a um administrador para incluir você, ou crie um projeto pela API.</p></div>`;
    return;
  }
  sel.innerHTML = projetos
    .map((p) => `<option value="${esc(p.slug)}">${esc(p.name)} · ${esc(p.role)}</option>`)
    .join("");
  estado.projetos = projetos;
  estado.projeto = projetos[0].slug;
  estado.papel = projetos[0].role;   // papel disponível em qualquer aba, já aqui
  // Projeto é a primeira aba e a de entrada. Ainda assim carregamos classes e
  // datasets aqui: estado.classes serve o anotador e estado.datasets serve o
  // escopo e o diálogo de treino, ambos usados por outras abas.
  $("botaoNovoProjeto").hidden = !estado.usuario.is_superuser;
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

/** Classes do projeto: o painel de anotação precisa delas. */
async function carregarProjeto() {
  const p = await api(`/projects/${estado.projeto}`);
  estado.classes = ((p.schema || {}).classes || []).map((c) => c.name);
}

// -------------------------------------------------------------- datasets --

async function carregarDatasets() {
  estado.datasets = await api(`/projects/${estado.projeto}/datasets`);
  const ul = $("listaDatasets");
  ul.innerHTML = estado.datasets.map((d) => `
    <li><button data-ds="${esc(d.slug)}" aria-current="${d.slug === estado.atual}">
      ${esc(d.name)}
      <span class="qtd">${d.versoes ? "v" + d.ultima_versao : "—"}</span>
    </button></li>`).join("");

  if (!estado.datasets.length) {
    estado.atual = null;
    $("painelDataset").innerHTML = `
      <div class="vazio">
        <p>Nenhum dataset neste projeto.</p>
        <p class="dica">Um dataset reúne origens do bucket e produz versões congeladas,
           que é o que torna dois treinos comparáveis.</p>
        <button class="primario" id="botaoPrimeiroDataset">Criar o primeiro</button>
      </div>`;
    $("botaoPrimeiroDataset").addEventListener(
      "click", () => $("dlgDataset").showModal());
    return;
  }
  await abrirDataset(estado.atual || estado.datasets[0].slug);
}

$("listaDatasets").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-ds]");
  if (b) abrirDataset(b.dataset.ds).catch(() => {});
});

async function abrirDataset(slug) {
  estado.atual = slug;
  document.querySelectorAll("[data-ds]").forEach((b) =>
    b.setAttribute("aria-current", b.dataset.ds === slug));
  $("painelDataset").innerHTML = `<p class="carregando">Carregando…</p>`;

  const base = `/projects/${estado.projeto}/datasets/${slug}`;
  try {
    const [ds, previa] = await Promise.all([api(base), api(`${base}/preview`)]);
    estado.previa = previa;
    render(ds, previa);
  } catch (e) {
    // Sem isto o painel fica em "Carregando…" para sempre e nenhum clique
    // seguinte recupera.
    $("painelDataset").innerHTML =
      `<div class="vazio"><p>Não consegui abrir este dataset.</p>
       <p class="dica">${esc(e.message)}</p>
       <button id="botaoTentarDeNovo">Tentar de novo</button></div>`;
    $("botaoTentarDeNovo").addEventListener("click", () => abrirDataset(slug));
  }
}

function render(ds, previa) {
  const podeCongelar = previa.entrariam > 0;
  $("painelDataset").innerHTML = `
    <div class="cabeca">
      <h1>${esc(ds.name)}</h1>
      <span class="tipo-ds ${esc(ds.kind)}">${esc(rotuloTipo(ds.kind))}</span>
      <span class="espaco"></span>
      ${ds.pretags_rascunho ? `<button class="discreto ruim" id="botaoDescartarPretags"
        title="remover os pré-tags rascunho não corrigidos">Descartar ${num(ds.pretags_rascunho)} pré-tags</button>` : ""}
      <button id="botaoRotular">Rotular estas imagens</button>
      <button class="primario" id="botaoCongelar" ${podeCongelar ? "" : "disabled"}>
        Congelar v${previa.proxima_versao}</button>
    </div>

    <section class="secao">
      <header>
        <span class="rotulo">Recorte (ROI)</span>
        <span class="espaco"></span>
        <button id="botaoRoi">${ds.roi ? "Editar recorte" : "Configurar recorte"}</button>
      </header>
      ${ds.roi ? `<p class="dica">Recorta cada imagem para
         <strong>x ${pctRoi(ds.roi.x)}–${pctRoi(ds.roi.x + ds.roi.w)}%,
         y ${pctRoi(ds.roi.y)}–${pctRoi(ds.roi.y + ds.roi.h)}%</strong> do quadro,
         aplicado no treino, avaliação e inferência. As próximas versões
         congelam com este recorte.</p>`
        : `<p class="dica">Sem recorte: treina no quadro inteiro. Configure quando só
             um alvo, sempre na mesma faixa, deve ser detectado — o recorte tira os
             vizinhos idênticos que senão confundem o modelo.</p>`}
    </section>

    <section class="secao">
      <header>
        <span class="rotulo">Origens</span>
        <span class="espaco"></span>
        <button id="botaoNovaOrigem">Adicionar origem</button>
      </header>
      ${ds.origens.length ? `<ul class="origens">${ds.origens.map(linhaOrigem).join("")}</ul>`
        : `<div class="vazio"><p>Nenhuma origem ainda.</p>
             <p class="dica">Aponte para um prefixo do bucket para trazer imagens.</p></div>`}
    </section>

    <section class="secao">
      <header><span class="rotulo">Prévia</span></header>
      <div class="previa">
        ${numero(previa.imagens_nas_origens, "nas origens")}
        ${numero(previa.anotadas, "anotadas")}
        ${numero(previa.pendentes, "sem anotação", previa.pendentes > 0)}
        ${numero(previa.entrariam, `entram na v${previa.proxima_versao}`)}
      </div>
      ${previa.sem_dimensao ? `<p class="dica">${num(previa.sem_dimensao)} imagens ainda
         sem dimensão registrada — só é descoberta ao abrir o arquivo.</p>` : ""}
    </section>

    <section class="secao">
      <header><span class="rotulo">Versões</span></header>
      ${ds.versoes.length ? `<ul class="trilho">${ds.versoes.map(linhaVersao).join("")}</ul>`
        : `<div class="vazio"><p>Nenhuma versão congelada.</p>
             <p class="dica">Congelar fixa a divisão treino/validação e grava o manifesto.
                A partir daí, aquele conjunto nunca muda.</p></div>`}
    </section>`;

  $("botaoCongelar").addEventListener("click", abrirCongelar);
  $("botaoRoi").addEventListener("click", () => abrirRoi(ds));
  if (ds.pretags_rascunho)
    $("botaoDescartarPretags").addEventListener("click",
      () => descartarPretags(ds.slug, ds.pretags_rascunho));
  $("botaoRotular").addEventListener("click", () => rotularDataset(ds.slug));
  $("botaoNovaOrigem").addEventListener("click",
    () => abrirOrigem().catch((e) => avisar(e.message, true)));
  $("painelDataset").querySelectorAll("[data-remover]").forEach((b) =>
    b.addEventListener("click", () => removerOrigem(b.dataset.remover, b.dataset.uri)));
  $("painelDataset").querySelectorAll("[data-diff]").forEach((b) =>
    b.addEventListener("click", () => verDiff(Number(b.dataset.diff))));
}

const rotuloTipo = (k) =>
  ({ train: "Treino", benchmark: "Benchmark", eval: "Avaliação" }[k] || k);

const numero = (v, k, destaque = false) => `
  <span class="numero ${destaque ? "pendente" : ""}">
    <span class="v">${num(v)}</span><span class="k rotulo">${esc(k)}</span></span>`;

const linhaOrigem = (o) => `
  <li>
    <span class="fonte-tipo">${esc(o.kind)}</span>
    <span class="uri">${esc(o.uri)}</span>
    <span class="imagens">${num(o.imagens)} img</span>
    <button class="discreto" data-remover="${esc(o.id)}" data-uri="${esc(o.uri)}"
            title="Remover da definição">Remover</button>
  </li>`;

const linhaVersao = (v) => `
  <li class="versao">
    <div class="versao-topo">
      <span class="versao-n">v${v.version}</span>
      <span class="selo">selada</span>
      ${v.version > 1 ? `<button class="discreto" data-diff="${v.version}">ver o que mudou</button>` : ""}
    </div>
    <p class="versao-desc">${esc(v.descricao || "")}</p>
    <div class="versao-meta">
      <span>${num(v.stats.imagens)} imagens</span>
      <span>${num(v.stats.train)} treino · ${num(v.stats.val)} validação</span>
      <span>${esc(v.split_policy.method === "by_group" ? "por grupo" : "aleatória")}</span>
      <span>${esc((v.criado_por || "").split("@")[0])}</span>
      <span>${new Date(v.frozen_at).toLocaleString("pt-BR")}</span>
    </div>
  </li>`;


/** Impede que o duplo clique dispare a ação duas vezes.
 *
 * Congelar é a operação mais lenta (resolve origens, sorteia split, grava
 * manifesto) e a mais cara de repetir: duas chamadas criam duas versões de um
 * objeto que o produto trata como imutável e auditável. */
function comTravaDeEnvio(form, acao) {
  return async (ev) => {
    ev.preventDefault();
    const botao = form.querySelector('button[value="ok"]');
    if (form.dataset.enviando) return;
    form.dataset.enviando = "1";
    if (botao) botao.disabled = true;
    try { await acao(ev); }
    finally { delete form.dataset.enviando; if (botao) botao.disabled = false; }
  };
}

// --------------------------------------------------------------- ações ---

$("botaoNovoDataset").addEventListener("click", () => $("dlgDataset").showModal());

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
    pjClasses = [];
  });
}

// Sugere o identificador a partir do nome, sem travar edição manual.
$("dsNome").addEventListener("input", (ev) => {
  const campo = $("dsSlug");
  if (campo.dataset.tocado) return;
  campo.value = ev.target.value.toLowerCase().normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "").replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
});
$("dsSlug").addEventListener("input", (ev) => { ev.target.dataset.tocado = "1"; });

$("formDataset").addEventListener("submit", comTravaDeEnvio($("formDataset"), async () => {
  try {
    await post(`/projects/${estado.projeto}/datasets`, {
      slug: $("dsSlug").value, name: $("dsNome").value, kind: $("dsTipo").value,
    });
    $("dlgDataset").close();
    estado.atual = $("dsSlug").value;
    $("formDataset").reset();
    delete $("dsSlug").dataset.tocado;
    avisar("Dataset criado.");
    await carregarDatasets();
  } catch (e) { avisar(e.message, true); }
}));

// ----------------------------------------------------------- recorte ROI --
//
// A ROI é uma caixa normalizada [x,y,w,h] em frações 0–1 que o pipeline recorta
// no treino/avaliação/inferência. A UI trabalha em presets (âncora 3×3 +
// largura%/altura%) e compila para a caixa; o backend guarda a caixa mais os
// campos de preset para repopular o diálogo.

const ANCHORS = ["topo-esq", "topo-centro", "topo-dir",
                 "meio-esq", "meio-centro", "meio-dir",
                 "base-esq", "base-centro", "base-dir"];
const pctRoi = (v) => Math.round(v * 100);
const roiEdit = { anchor: "topo-centro", w: 40, h: 40, slug: null };

/** Preset (âncora + largura%/altura%) → caixa normalizada {x,y,w,h}. */
function compilarRoi(anchor, wPct, hPct) {
  const w = wPct / 100, h = hPct / 100;
  const [ver, hor] = anchor.split("-");
  const x = hor === "esq" ? 0 : hor === "dir" ? 1 - w : (1 - w) / 2;
  const y = ver === "topo" ? 0 : ver === "base" ? 1 - h : (1 - h) / 2;
  const r4 = (v) => Math.round(v * 1e4) / 1e4;
  return { x: r4(x), y: r4(y), w: r4(w), h: r4(h),
           anchor, w_pct: r4(w), h_pct: r4(h) };
}

/** Caixa salva sem preset (ex.: derivada de rótulos) → âncora mais próxima. */
function derivarAnchor(r) {
  const cx = r.x + r.w / 2, cy = r.y + r.h / 2;
  const faixa = (c) => c < 1 / 3 ? 0 : c > 2 / 3 ? 2 : 1;   // terços
  const hor = ["esq", "centro", "dir"][faixa(cx)];
  const ver = ["topo", "meio", "base"][faixa(cy)];
  return `${ver}-${hor}`;
}

function abrirRoi(ds) {
  const r = ds.roi;
  if (r) {
    roiEdit.w = Math.round((r.w_pct != null ? r.w_pct : r.w) * 100);
    roiEdit.h = Math.round((r.h_pct != null ? r.h_pct : r.h) * 100);
    roiEdit.anchor = ANCHORS.includes(r.anchor) ? r.anchor : derivarAnchor(r);
  } else {
    roiEdit.anchor = "topo-centro"; roiEdit.w = 40; roiEdit.h = 40;
  }
  roiEdit.slug = ds.slug;
  sincronizarRoiUI();
  $("dlgRoi").showModal();
}

function sincronizarRoiUI() {
  $("roiGrade").querySelectorAll("[data-anchor]").forEach((b) =>
    b.setAttribute("aria-current", b.dataset.anchor === roiEdit.anchor));
  $("roiLarg").value = roiEdit.w;
  $("roiAlt").value = roiEdit.h;
  $("roiLargV").textContent = roiEdit.w + "%";
  $("roiAltV").textContent = roiEdit.h + "%";
  desenharRoiPreview();
}

/** Desenha a ROI (clara) sobre um quadro retrato, com o fora escurecido e
 *  hachurado — o mesmo vocabulário do overlay na rotulagem. */
function desenharRoiPreview() {
  const cv = $("roiPreview"), cx = cv.getContext("2d");
  const roi = compilarRoi(roiEdit.anchor, roiEdit.w, roiEdit.h);
  const x = roi.x * cv.width, y = roi.y * cv.height,
        w = roi.w * cv.width, h = roi.h * cv.height;
  cx.clearRect(0, 0, cv.width, cv.height);
  cx.fillStyle = "#0b0f14";
  cx.fillRect(0, 0, cv.width, cv.height);
  hachurarFora(cx, cv.width, cv.height, x, y, w, h);
  cx.strokeStyle = "#ffd60a";
  cx.lineWidth = 2;
  cx.strokeRect(x, y, w, h);
  cx.strokeStyle = "#2a3540";
  cx.lineWidth = 1;
  cx.strokeRect(0.5, 0.5, cv.width - 1, cv.height - 1);
  $("roiCoords").textContent =
    `x ${pctRoi(roi.x)}–${pctRoi(roi.x + roi.w)}% · ` +
    `y ${pctRoi(roi.y)}–${pctRoi(roi.y + roi.h)}%`;
}

/** Escurece e hachura a área FORA da caixa (rx,ry,rw,rh) num canvas. */
function hachurarFora(cx, W, H, rx, ry, rw, rh) {
  cx.save();
  cx.beginPath();
  cx.rect(0, 0, W, H);
  cx.rect(rx, ry, rw, rh);
  cx.clip("evenodd");                     // recorta para só-o-fora
  cx.fillStyle = "rgba(5,8,11,.55)";
  cx.fillRect(0, 0, W, H);
  cx.strokeStyle = "rgba(255,214,10,.22)";
  cx.lineWidth = 1;
  for (let d = -H; d < W; d += 8) {
    cx.beginPath(); cx.moveTo(d, 0); cx.lineTo(d + H, H); cx.stroke();
  }
  cx.restore();
}

$("roiGrade").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-anchor]");
  if (!b) return;
  roiEdit.anchor = b.dataset.anchor;
  sincronizarRoiUI();
});
$("roiLarg").addEventListener("input", (ev) => {
  roiEdit.w = Number(ev.target.value); sincronizarRoiUI();
});
$("roiAlt").addEventListener("input", (ev) => {
  roiEdit.h = Number(ev.target.value); sincronizarRoiUI();
});

async function salvarRoi(roi) {
  await api(`/projects/${estado.projeto}/datasets/${roiEdit.slug}`,
            { method: "PATCH", body: JSON.stringify({ roi }) });
  $("dlgRoi").close();
  avisar(roi ? "Recorte salvo." : "Recorte removido.");
  await abrirDataset(roiEdit.slug);
}

$("roiRemover").addEventListener("click", () =>
  salvarRoi(null).catch((e) => avisar(e.message, true)));

$("formRoi").addEventListener("submit", comTravaDeEnvio($("formRoi"), async () => {
  try {
    await salvarRoi(compilarRoi(roiEdit.anchor, roiEdit.w, roiEdit.h));
  } catch (e) { avisar(e.message, true); }
}));

// --------------------------------------------------- navegador de bucket --

/** Abre o diálogo já com os buckets da instalação e na raiz do primeiro. */
async function abrirOrigem() {
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
    await esperarJob(r.job_id);
    await api(`/projects/${estado.projeto}/datasets/${estado.atual}` +
              `/origens/${r.source_id}`, { method: "PUT" });
    $("formOrigem").reset();
    await abrirDataset(estado.atual);
  } catch (e) { avisar(e.message, true); }
}));

/** A importação roda como job; acompanha até terminar. */
async function esperarJob(jobId) {
  const url = `/projects/${estado.projeto}/jobs/${jobId}`;
  for (let i = 0; i < 600; i++) {
    const j = await api(url);
    if (j.status === "succeeded") {
      const r = j.result || {};
      avisar(`${num(r.samples_created)} imagens novas · ${num(r.samples_existing)} já existiam`);
      return j;
    }
    if (j.status === "failed") throw new Error(j.error || "importação falhou");
    if (j.progress && j.progress.lidos) avisar(`${num(j.progress.lidos)} lidas…`);
    await new Promise((r) => setTimeout(r, 600));
  }
  throw new Error("importação demorou demais — acompanhe pelos jobs");
}

async function removerOrigem(id, uri) {
  if (!confirm(`Remover ${uri} deste dataset?\n\n` +
      "As imagens e anotações continuam no projeto, e as versões já congeladas " +
      "não mudam. Só as próximas versões deixam de incluir essas imagens."))
    return;
  try {
    await api(`/projects/${estado.projeto}/datasets/${estado.atual}/origens/${id}`,
              { method: "DELETE" });
    avisar("Origem removida da definição.");
    await abrirDataset(estado.atual);
  } catch (e) { avisar(e.message, true); }
}

async function descartarPretags(slug, n) {
  if (!confirm(`Descartar ${n} pré-tags (rascunho) deste dataset?\n\n` +
      "Remove só as caixas geradas pelo modelo que ninguém corrigiu. " +
      "Trabalho humano e pré-tags já corrigidos ficam intactos. Sem volta."))
    return;
  try {
    const r = await post(
      `/projects/${estado.projeto}/datasets/${slug}/prelabel/discard`, {});
    avisar(`${num(r.removidos)} pré-tags removidos.`);
    await abrirDataset(slug);
  } catch (e) { avisar(e.message, true); }
}

function abrirCongelar() {
  const p = estado.previa;
  $("congelarResumo").textContent =
    `${num(p.entrariam)} imagens entram na v${p.proxima_versao}. ` +
    `A divisão treino/validação é sorteada agora e fica gravada — ` +
    `é o que torna dois treinos comparáveis.`;
  $("dlgCongelar").showModal();
}

$("formCongelar").addEventListener("submit", comTravaDeEnvio($("formCongelar"), async () => {
  try {
    const r = await post(
      `/projects/${estado.projeto}/datasets/${estado.atual}/freeze`, {
        descricao: $("cgDesc").value,
        val_ratio: Number($("cgVal").value),
        split_method: $("cgSplit").value,
      });
    $("dlgCongelar").close();
    $("formCongelar").reset();
    avisar(`v${r.version} congelada · ${num(r.stats.imagens)} imagens`);
    await abrirDataset(estado.atual);
  } catch (e) { avisar(e.message, true); }
}));

async function verDiff(versao) {
  try {
    const d = await api(
      `/projects/${estado.projeto}/datasets/${estado.atual}/versions/${versao}/diff`);
    const lista = (t, arr) => arr.length
      ? `${t} (${arr.length}):\n` + arr.slice(0, 25).join("\n") +
        (arr.length > 25 ? `\n… e mais ${arr.length - 25}` : "")
      : `${t}: nenhuma`;
    alert(`v${d.de} → v${d.para}\n\n` +
          lista("Entraram", d.entraram) + "\n\n" + lista("Saíram", d.sairam));
  } catch (e) { avisar(e.message, true); }
}

// Só 401 leva ao login — e disso `api()` já cuida. Tratar qualquer erro como
// falha de autenticação criava um laço: o usuário logava, a carga quebrava no
// mesmo ponto e ele voltava para o login, sem nunca ver a causa.
iniciar().catch((e) => avisar(e.message, true));

// =========================================================== aba Dados ====
//
// Grade de imagens + canvas de anotação. As imagens vêm assinadas e são
// buscadas direto do GCS: a API emite a URL e não trafega bytes.

const dados = { pagina: 0, filtro: "todas", dataset: "",
                itens: [], total: 0, assina: false };

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
      <dt>Papéis</dt><dd><b>admin</b> gerencia tudo; <b>engineer</b> treina,
        compila e implanta modelos; <b>annotator</b> só rotula imagens.</dd>
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
      <dt>Papéis</dt><dd><b>admin</b> gerencia projetos e pessoas;
        <b>engineer</b> treina/compila/implanta; <b>annotator</b> rotula.</dd>
      <dt>Membro do projeto</dt><dd>O papel vale por projeto: a mesma pessoa
        pode ser engineer num projeto e annotator em outro.</dd>
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
function mostrarAba(nome) {
  document.querySelectorAll("#abas [data-aba]").forEach((x) =>
    x.setAttribute("aria-current", x.dataset.aba === nome));
  for (const [aba, el] of Object.entries({
    projeto: "abaProjeto", datasets: "abaDatasets", dados: "abaDados",
    treino: "abaTreino", modelos: "abaModelos", devices: "abaDevices",
    usuarios: "abaUsuarios",
  })) $(el).hidden = aba !== nome;
  // Sair da aba de treino encerra o polling ao vivo — senão ele segue batendo
  // na API em segundo plano por uma tela que ninguém está vendo.
  if (nome !== "treino") pararPollRun();
  if (nome === "projeto") carregarProjetoTab().catch(erroAviso);
  if (nome === "dados" && !dados.itens.length) carregarDados().catch(erroAviso);
  if (nome === "treino") carregarRuns().catch(erroAviso);
  if (nome === "modelos") carregarModelos().catch(erroAviso);
  if (nome === "devices") carregarDevices().catch(erroAviso);
  if (nome === "usuarios") carregarUsuarios().catch(erroAviso);
}

function preencherEscopo() {
  const sel = $("escopoDataset");
  const atual = dados.dataset;
  sel.innerHTML = `<option value="">Todas as imagens</option>` +
    estado.datasets.map((d) =>
      `<option value="${esc(d.slug)}">${esc(d.name)}</option>`).join("");
  // o dataset escolhido pode ter sumido (troca de projeto): volta p/ "todas"
  sel.value = estado.datasets.some((d) => d.slug === atual) ? atual : "";
  dados.dataset = sel.value;
  atualizarBotaoPretag();
}

$("escopoDataset").addEventListener("change", (ev) => {
  dados.dataset = ev.target.value;
  dados.pagina = 0;
  atualizarBotaoPretag();
  carregarDados().catch(erroAviso);
});

$("filtros").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-filtro]");
  if (!b) return;
  document.querySelectorAll("#filtros [data-filtro]").forEach((x) =>
    x.setAttribute("aria-current", x === b));
  dados.filtro = b.dataset.filtro;
  dados.pagina = 0;
  carregarDados().catch(erroAviso);
});

$("paginaAnterior").addEventListener("click", () => {
  if (dados.pagina > 0) { dados.pagina--; carregarDados().catch(erroAviso); }
});
$("paginaProxima").addEventListener("click", () => {
  dados.pagina++; carregarDados().catch(erroAviso);
});

const erroAviso = (e) => avisar(e.message, true);

async function carregarDados() {
  $("grade").innerHTML = `<p class="carregando">Carregando…</p>`;
  const q = `?pagina=${dados.pagina}&filtro=${dados.filtro}` +
            (dados.dataset ? `&dataset=${encodeURIComponent(dados.dataset)}` : "");
  const d = await api(`/projects/${estado.projeto}/samples${q}`);
  Object.assign(dados, d, { itens: d.itens });

  $("contagemDados").textContent =
    `${num(d.total)} imagens · página ${d.pagina + 1}`;
  $("paginaAnterior").disabled = d.pagina === 0;
  $("paginaProxima").disabled = d.itens.length < d.por_pagina;

  if (!d.itens.length) {
    $("grade").innerHTML = `<div class="vazio"><p>Nenhuma imagem aqui.</p>
      <p class="dica">Catalogue um prefixo do bucket pela aba Datasets,
         em "Adicionar origem".</p></div>`;
    return;
  }
  $("grade").innerHTML = d.itens.map(cartao).join("");
  $("grade").querySelectorAll("[data-abrir]").forEach((b) =>
    b.addEventListener("click", () => abrirAnotador(b.dataset.abrir)));
  if (!d.assina) avisar("sem miniaturas: a credencial não assina URL", true);
}

function cartao(it) {
  const nome = it.uri.split("/").pop();
  // "0" é informação: significa "olhei e não há nada", não "não olhei".
  const estadoCaixa = it.annotation_id === null
    ? `<span class="marca-estado">—</span>`
    : (it.n_caixas === 0
        ? `<span class="marca-estado vazia">vazia</span>`
        : `<span class="marca-estado feita">${it.n_caixas}</span>`);
  return `<button class="cartao" data-abrir="${esc(it.id)}" title="${esc(nome)}">
    ${it.url ? `<img src="${esc(it.url)}" alt="" loading="lazy">`
             : `<span class="sem-imagem">sem miniatura</span>`}
    ${estadoCaixa}<span class="nome">${esc(nome)}</span></button>`;
}

// ------------------------------------------------------------- anotador --

const anot = { id: null, boxes: [], classe: 0, classes: [], sel: -1,
               desenhando: null, img: null, indice: -1, sujo: false,
               // preferência lembrada: quem rotula o dia todo não quer
               // remarcar a caixinha a cada sessão
               nomes: localStorage.getItem("visionyard.nomes") !== "0",
               arrasto: null, fit: 1, zoom: 1, roi: null };

const CORES = ["#00d26a", "#ff8a1f", "#3aa0ff", "#e5484d", "#c77dff",
               "#ffd60a", "#00c2c7", "#ff6fb5"];
const cor = (i) => CORES[i % CORES.length];

async function abrirAnotador(id) {
  const d = await api(`/projects/${estado.projeto}/samples/${id}`);
  anot.id = id;
  anot.classes = (estado.classes || []).map((c, i) => ({ nome: c, cor: cor(i) }));
  anot.boxes = (d.minha_anotacao && d.minha_anotacao.boxes) || [];
  anot.sel = -1;
  anot.classe = 0;
  anot.sujo = false;
  anot.indice = dados.itens.findIndex((x) => x.id === id);
  $("anotNomes").checked = anot.nomes;

  const dsAtual = estado.datasets.find((x) => x.slug === dados.dataset);
  // A ROI só existe quando um dataset está no escopo da rotulagem. O overlay é
  // informativo: mostra o que o treino vai recortar; não trava o desenho.
  anot.roi = (dsAtual && dsAtual.roi) || null;
  const badge = $("anotDataset");
  if (dsAtual) { badge.textContent = dsAtual.name; badge.hidden = false; }
  else { badge.hidden = true; }
  $("anotArquivo").textContent = d.uri.split("/").pop();
  $("anotOutros").textContent = d.outros_anotaram
    ? `${d.outros_anotaram} outro(s) rotulador(es) já anotaram esta imagem. ` +
      `As caixas deles não aparecem: ver antes de desenhar enviesa a sua.`
    : "";
  $("anotDica").innerHTML =
    `<span class="anot-progresso">${anot.indice + 1}/${dados.itens.length}` +
    ` · pág ${dados.pagina + 1} · arraste as alças p/ ajustar</span>`;
  $("anotador").hidden = false;

  const img = $("anotImagem");
  anot.img = img;
  await new Promise((ok, falha) => {
    img.onload = ok;
    img.onerror = () => falha(new Error("não consegui carregar a imagem"));
    img.src = d.url || "";
    if (!d.url) falha(new Error("imagem sem URL assinada"));
  }).catch((e) => avisar(e.message, true));

  // Cada imagem abre inteira (fit) e sem zoom; a roda amplia a partir daí.
  anot.zoom = 1;
  calcularFit();
  renderImagem();
  $("anotTela").scrollTo(0, 0);
  desenharClasses();
}

/** Escala que faz a imagem inteira caber na área, sem cortar. */
function calcularFit() {
  const img = $("anotImagem");
  const tela = $("anotTela").getBoundingClientRect();
  if (!img.naturalWidth || !tela.width) { anot.fit = 1; return; }
  anot.fit = Math.min(tela.width / img.naturalWidth,
                      tela.height / img.naturalHeight);
}

/** Aplica fit × zoom: dimensiona imagem, wrapper e canvas juntos. O canvas
 *  fica em resolução 1:1 com o tamanho exibido, então o mapeamento de
 *  ponteiro (posCanvas) não muda — só a escala. */
function renderImagem() {
  const img = $("anotImagem"), cv = $("anotCanvas"), zoom = $("anotZoom");
  if (!img.naturalWidth) return;
  const w = Math.round(img.naturalWidth * anot.fit * anot.zoom);
  const h = Math.round(img.naturalHeight * anot.fit * anot.zoom);
  img.style.width = zoom.style.width = w + "px";
  img.style.height = zoom.style.height = h + "px";
  cv.width = w; cv.height = h;
  cv.style.width = w + "px"; cv.style.height = h + "px";
  redesenhar();
}

window.addEventListener("resize", () => {
  if ($("anotador").hidden) return;
  calcularFit();
  anot.zoom = Math.max(1, anot.zoom);
  renderImagem();
});

// Roda do mouse dá zoom, mantendo sob o cursor o mesmo ponto da imagem.
$("anotTela").addEventListener("wheel", (ev) => {
  if ($("anotador").hidden) return;
  ev.preventDefault();
  const tela = $("anotTela");
  const antes = anot.zoom;
  const fator = ev.deltaY < 0 ? 1.15 : 1 / 1.15;
  anot.zoom = clamp(anot.zoom * fator, 1, 12);
  if (anot.zoom === antes) return;

  // ponto da imagem sob o cursor, antes de reescalar
  const cv = $("anotCanvas"), r = cv.getBoundingClientRect();
  const fx = (ev.clientX - r.left) / r.width;
  const fy = (ev.clientY - r.top) / r.height;
  renderImagem();
  // reposiciona o scroll para o mesmo ponto ficar sob o cursor
  const telaR = tela.getBoundingClientRect();
  tela.scrollLeft = fx * cv.width - (ev.clientX - telaR.left);
  tela.scrollTop = fy * cv.height - (ev.clientY - telaR.top);
}, { passive: false });

function desenharClasses() {
  $("anotClasses").innerHTML = anot.classes.map((c, i) => `
    <li><button data-classe="${i}" aria-current="${i === anot.classe}">
      <span class="cor" style="background:${c.cor}"></span>
      ${esc(c.nome)}<span class="tecla">${i + 1}</span></button></li>`).join("");
  $("anotClasses").querySelectorAll("[data-classe]").forEach((b) =>
    b.addEventListener("click", () => {
      anot.classe = Number(b.dataset.classe);
      // trocar a classe com uma caixa selecionada reclassifica ela
      if (anot.sel >= 0) { anot.boxes[anot.sel].cls = anot.classe;
                           anot.sujo = true; redesenhar(); }
      desenharClasses();
    }));
}

function listarCaixas() {
  $("anotCaixas").innerHTML = anot.boxes.length
    ? anot.boxes.map((b, i) => `
        <li class="${i === anot.sel ? "selecionada" : ""}" data-caixa="${i}">
          <span class="cor" style="background:${cor(b.cls)}"></span>
          ${esc((anot.classes[b.cls] || {}).nome || b.cls)}
          <button class="discreto" data-apagar="${i}">apagar</button></li>`).join("")
    : `<li class="vazio">nenhuma caixa — salvar assim marca a imagem como
         "olhei, não há nada"</li>`;
  $("anotCaixas").querySelectorAll("[data-caixa]").forEach((li) =>
    li.addEventListener("click", (ev) => {
      if (ev.target.dataset.apagar !== undefined) return;
      anot.sel = Number(li.dataset.caixa); redesenhar();
    }));
  $("anotCaixas").querySelectorAll("[data-apagar]").forEach((b) =>
    b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      anot.boxes.splice(Number(b.dataset.apagar), 1);
      anot.sel = -1; anot.sujo = true; redesenhar();
    }));
}

function redesenhar() {
  const cv = $("anotCanvas"), cx = cv.getContext("2d");
  cx.clearRect(0, 0, cv.width, cv.height);
  // Recorte do dataset em escopo: escurece e hachura o que o treino vai jogar
  // fora, e contorna a ROI. Fica sob as caixas, que seguem visíveis por cima.
  if (anot.roi) {
    const r = anot.roi;
    const rx = r.x * cv.width, ry = r.y * cv.height,
          rw = r.w * cv.width, rh = r.h * cv.height;
    hachurarFora(cx, cv.width, cv.height, rx, ry, rw, rh);
    cx.strokeStyle = "#ffd60a";
    cx.lineWidth = 2;
    cx.setLineDash([6, 4]);
    cx.strokeRect(rx, ry, rw, rh);
    cx.setLineDash([]);
  }
  anot.boxes.forEach((b, i) => {
    const x = (b.cx - b.w / 2) * cv.width, y = (b.cy - b.h / 2) * cv.height;
    const w = b.w * cv.width, h = b.h * cv.height;
    cx.lineWidth = i === anot.sel ? 3 : 2;
    cx.strokeStyle = cor(b.cls);
    cx.strokeRect(x, y, w, h);
    if (i === anot.sel) {
      cx.fillStyle = cor(b.cls) + "22";
      cx.fillRect(x, y, w, h);
    }
    if (!anot.nomes) return;
    const nome = (anot.classes[b.cls] || {}).nome || String(b.cls);
    cx.font = "600 11px ui-monospace, monospace";
    const larg = cx.measureText(nome).width + 8;
    // acima da caixa; se não couber (caixa colada no topo), vai para dentro
    const acima = y >= 15;
    cx.fillStyle = cor(b.cls);
    cx.fillRect(x, acima ? y - 15 : y, larg, 15);
    cx.fillStyle = "#0d1217";
    cx.fillText(nome, x + 4, (acima ? y - 4 : y + 11));
  });
  // Alças de resize na caixa selecionada — só nela, para não poluir a tela.
  if (anot.sel >= 0) {
    const b = anot.boxes[anot.sel];
    for (const [hx, hy] of pontosAlca(b, cv)) {
      cx.fillStyle = "#0d1217";
      cx.strokeStyle = cor(b.cls);
      cx.lineWidth = 2;
      cx.fillRect(hx - ALCA, hy - ALCA, ALCA * 2, ALCA * 2);
      cx.strokeRect(hx - ALCA, hy - ALCA, ALCA * 2, ALCA * 2);
    }
  }
  if (anot.desenhando) {
    const d = anot.desenhando;
    cx.setLineDash([4, 3]);
    cx.strokeStyle = cor(anot.classe);
    cx.lineWidth = 2;
    cx.strokeRect(d.x0, d.y0, d.x1 - d.x0, d.y1 - d.y0);
    cx.setLineDash([]);
  }
  listarCaixas();
}

const ALCA = 5;  // meio-lado da alça, em px

// As 8 alças em px, junto com o código de qual borda cada uma move.
// n/s/e/w = norte/sul/leste/oeste; as diagonais combinam dois.
function pontosAlca(b, cv) {
  const x1 = (b.cx - b.w / 2) * cv.width, y1 = (b.cy - b.h / 2) * cv.height;
  const x2 = (b.cx + b.w / 2) * cv.width, y2 = (b.cy + b.h / 2) * cv.height;
  const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
  return [[x1, y1, "nw"], [mx, y1, "n"], [x2, y1, "ne"],
          [x2, my, "e"], [x2, y2, "se"], [mx, y2, "s"],
          [x1, y2, "sw"], [x1, my, "w"]];
}

const CURSOR = {nw: "nwse-resize", se: "nwse-resize", ne: "nesw-resize",
                sw: "nesw-resize", n: "ns-resize", s: "ns-resize",
                e: "ew-resize", w: "ew-resize", mover: "move"};

const posCanvas = (ev) => {
  const r = $("anotCanvas").getBoundingClientRect();
  return { x: ev.clientX - r.left, y: ev.clientY - r.top };
};

/** O que está sob o ponto: uma alça da caixa selecionada, o corpo de alguma
 *  caixa (para mover), ou nada (para desenhar). */
function alvoEm(p, cv) {
  if (anot.sel >= 0) {
    for (const [hx, hy, borda] of pontosAlca(anot.boxes[anot.sel], cv)) {
      if (Math.abs(p.x - hx) <= ALCA + 2 && Math.abs(p.y - hy) <= ALCA + 2)
        return { tipo: "resize", borda };
    }
  }
  // topo primeiro: a caixa desenhada por último fica por cima
  for (let i = anot.boxes.length - 1; i >= 0; i--) {
    const b = anot.boxes[i];
    if (Math.abs(p.x / cv.width - b.cx) < b.w / 2 &&
        Math.abs(p.y / cv.height - b.cy) < b.h / 2)
      return { tipo: "mover", indice: i };
  }
  return { tipo: "desenhar" };
}

$("anotCanvas").addEventListener("pointerdown", (ev) => {
  const cv = $("anotCanvas"), p = posCanvas(ev);
  const alvo = alvoEm(p, cv);

  if (alvo.tipo === "resize") {
    anot.arrasto = { modo: "resize", borda: alvo.borda };
  } else if (alvo.tipo === "mover" && !ev.shiftKey) {
    anot.sel = alvo.indice;
    const b = anot.boxes[anot.sel];
    // guarda o deslocamento do ponto ao centro, para mover sem "pular"
    anot.arrasto = { modo: "mover",
                     dx: p.x / cv.width - b.cx, dy: p.y / cv.height - b.cy };
  } else {
    anot.desenhando = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
  }
  ev.target.setPointerCapture(ev.pointerId);
  redesenhar();
});

$("anotCanvas").addEventListener("pointermove", (ev) => {
  const cv = $("anotCanvas"), p = posCanvas(ev);
  const fx = clamp01(p.x / cv.width), fy = clamp01(p.y / cv.height);

  if (anot.desenhando) {
    // Prende o desenho às bordas da imagem: arrastar para fora não estica a
    // caixa além dela. (Move e resize já são presos abaixo.)
    anot.desenhando.x1 = clamp(p.x, 0, cv.width);
    anot.desenhando.y1 = clamp(p.y, 0, cv.height);
    redesenhar();
  } else if (anot.arrasto && anot.arrasto.modo === "mover") {
    const b = anot.boxes[anot.sel];
    // mantém a caixa inteira dentro da imagem
    b.cx = clamp(fx - anot.arrasto.dx, b.w / 2, 1 - b.w / 2);
    b.cy = clamp(fy - anot.arrasto.dy, b.h / 2, 1 - b.h / 2);
    anot.sujo = true; redesenhar();
  } else if (anot.arrasto && anot.arrasto.modo === "resize") {
    redimensionar(anot.boxes[anot.sel], anot.arrasto.borda, fx, fy);
    anot.sujo = true; redesenhar();
  } else {
    // sem arrasto: só ajusta o cursor conforme o que está sob o ponteiro
    const alvo = alvoEm(p, cv);
    cv.style.cursor = alvo.tipo === "resize" ? CURSOR[alvo.borda]
      : alvo.tipo === "mover" ? CURSOR.mover : "crosshair";
  }
});

$("anotCanvas").addEventListener("pointerup", () => {
  if (anot.arrasto) {
    // resize por um lado só pode ter cruzado o oposto: normaliza w/h > 0
    if (anot.sel >= 0) normalizarCaixa(anot.boxes[anot.sel]);
    anot.arrasto = null; redesenhar(); return;
  }
  const d = anot.desenhando;
  anot.desenhando = null;
  if (!d) return;
  const cv = $("anotCanvas");
  const x = Math.min(d.x0, d.x1), y = Math.min(d.y0, d.y1);
  const w = Math.abs(d.x1 - d.x0), h = Math.abs(d.y1 - d.y0);
  // arrasto minúsculo é clique numa área vazia: só limpa a seleção
  if (w < 6 || h < 6) { anot.sel = -1; redesenhar(); return; }
  anot.boxes.push({
    cls: anot.classe,
    cx: clamp01((x + w / 2) / cv.width), cy: clamp01((y + h / 2) / cv.height),
    w: Math.min(1, w / cv.width), h: Math.min(1, h / cv.height),
  });
  anot.sel = anot.boxes.length - 1;
  anot.sujo = true;
  redesenhar();
});

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const clamp01 = (v) => clamp(v, 0, 1);

/** Move a(s) borda(s) indicada(s) para o ponto (fx,fy), em coordenadas 0..1.
 *  Trabalha em cantos e reconstrói centro/tamanho, para o arrasto ser
 *  intuitivo mesmo quando um lado ultrapassa o outro. */
function redimensionar(b, borda, fx, fy) {
  let e = b.cx - b.w / 2, o = b.cx + b.w / 2;   // esquerda, direita
  let n = b.cy - b.h / 2, s = b.cy + b.h / 2;   // topo, base
  if (borda.includes("w")) e = fx;
  if (borda.includes("e")) o = fx;
  if (borda.includes("n")) n = fy;
  if (borda.includes("s")) s = fy;
  b.cx = (e + o) / 2; b.cy = (n + s) / 2;
  b.w = Math.abs(o - e); b.h = Math.abs(s - n);
}

/** Depois de um resize que cruzou o lado oposto, garante w/h mínimos e a
 *  caixa dentro da imagem. */
function normalizarCaixa(b) {
  b.w = clamp(b.w, 0.004, 1); b.h = clamp(b.h, 0.004, 1);
  b.cx = clamp(b.cx, b.w / 2, 1 - b.w / 2);
  b.cy = clamp(b.cy, b.h / 2, 1 - b.h / 2);
}

document.addEventListener("keydown", (ev) => {
  if ($("anotador").hidden) return;
  // `matches` só existe em Element: quando o foco está no próprio documento
  // (nada focado, ou evento sintético), `ev.target` é o Document e a chamada
  // estoura — derrubando o atalho inteiro.
  if (ev.target instanceof Element &&
      ev.target.matches("input, textarea, select")) return;
  const k = ev.key.toLowerCase();

  if (ev.key === "Escape") { fecharAnotador(); return; }

  if (k === "d" || ev.key === "Delete" || ev.key === "Backspace") {
    if (anot.sel < 0) return;
    ev.preventDefault();
    anot.boxes.splice(anot.sel, 1);
    anot.sel = -1; anot.sujo = true; redesenhar();
    return;
  }
  if (k === "q") { ev.preventDefault(); irParaImagem(-1).catch(erroAviso); return; }
  if (k === "e") { ev.preventDefault(); irParaImagem(+1).catch(erroAviso); return; }
  if (k === "w") { ev.preventDefault(); prerotular(); return; }

  const n = Number(ev.key);
  if (n >= 1 && n <= anot.classes.length) {
    anot.classe = n - 1;
    if (anot.sel >= 0) { anot.boxes[anot.sel].cls = anot.classe; anot.sujo = true; }
    desenharClasses(); redesenhar();
  }
});

/** Anterior/próxima dentro da página, buscando a página vizinha na borda.
 *
 * Salva antes de sair SÓ se algo mudou: navegar sem alterar nada não pode
 * gravar uma anotação vazia, que no modelo significa "olhei e não há nada".
 */
async function irParaImagem(passo) {
  if (anot.sujo) await salvarAnotacao({ silencioso: true });

  let i = anot.indice + passo;
  if (i < 0 || i >= dados.itens.length) {
    const alvo = dados.pagina + (passo > 0 ? 1 : -1);
    if (alvo < 0) { avisar("primeira imagem"); return; }
    const antes = dados.itens.length;
    dados.pagina = alvo;
    await carregarDados();
    if (!dados.itens.length) {
      dados.pagina -= (passo > 0 ? 1 : -1);
      await carregarDados();
      avisar(passo > 0 ? "última imagem" : "primeira imagem");
      return;
    }
    i = passo > 0 ? 0 : dados.itens.length - 1;
    void antes;
  }
  await abrirAnotador(dados.itens[i].id);
}

/** Tecla w: roda o modelo NESTA imagem e traz as caixas para corrigir.
 *
 * É um pré-tag de escopo-de-imagem: enfileira, espera o agente prever, e carrega
 * o resultado no canvas. Só faz sentido numa imagem em branco — o modelo não
 * sobrescreve o que você já desenhou. */
async function prerotular() {
  if (anot.boxes.length) {
    avisar("esta imagem já tem caixas — o pré-tag é para começar do zero");
    return;
  }
  const modelos = await api(`/projects/${estado.projeto}/models`).catch(() => []);
  if (!modelos.length) {
    avisar("nenhum modelo treinado ainda — treine um na aba Treinamento", true);
    return;
  }
  // prefere quem tem champion; senão o primeiro modelo
  const m = modelos.find((x) => x.aliases && x.aliases.champion) || modelos[0];
  const btn = $("anotPretag");
  const rotulo = btn.textContent;
  btn.disabled = true; btn.textContent = "…";
  try {
    const r = await post(`/projects/${estado.projeto}/prelabel`, {
      model_slug: m.slug, sample_id: anot.id, conf: 0.25, only_missing: false,
    });
    avisar(`rodando ${m.name} v${r.model_version} nesta imagem…`);
    if (!await esperarPrelabelSample(r.job_id)) return;
    const d = await api(`/projects/${estado.projeto}/samples/${anot.id}`);
    const boxes = (d.minha_anotacao && d.minha_anotacao.boxes) || [];
    if (!boxes.length) { avisar("o modelo não encontrou nada aqui"); return; }
    anot.boxes = boxes; anot.sel = -1; anot.sujo = false; redesenhar();
    avisar(`${boxes.length} caixa(s) do modelo — corrija e salve`);
  } catch (e) { avisar(e.message, true); }
  finally { btn.disabled = false; btn.textContent = rotulo; }
}

async function esperarPrelabelSample(jobId) {
  const url = `/projects/${estado.projeto}/jobs/${jobId}`;
  for (let i = 0; i < 120; i++) {
    let j;
    try { j = await api(url); } catch { return false; }
    if (j.status === "succeeded") return true;
    if (j.status === "failed") { avisar(j.error || "pré-tag falhou", true); return false; }
    if (j.status === "canceled") return false;
    await new Promise((r) => setTimeout(r, 1200));
  }
  avisar("o pré-tag demorou demais — há uma máquina de inferência no ar?", true);
  return false;
}

$("anotNomes").addEventListener("change", (ev) => {
  anot.nomes = ev.target.checked;
  localStorage.setItem("visionyard.nomes", anot.nomes ? "1" : "0");
  redesenhar();
});
$("anotAnterior").addEventListener("click",
  () => irParaImagem(-1).catch(erroAviso));
$("anotPretag").addEventListener("click", prerotular);

$("anotVoltar").addEventListener("click", fecharAnotador);
function fecharAnotador() {
  $("anotador").hidden = true;
  $("anotImagem").src = "";
  anot.arrasto = null;
  $("anotCanvas").style.cursor = "crosshair";
}

async function salvarAnotacao({ silencioso = false } = {}) {
  await api(`/projects/${estado.projeto}/samples/${anot.id}/annotation`,
            { method: "PUT",
              body: JSON.stringify({ boxes: anot.boxes, status: "submitted" }) });
  anot.sujo = false;
  // reflete na grade sem recarregar a página inteira
  const item = dados.itens[anot.indice];
  if (item) { item.annotation_id = item.annotation_id || "salva";
              item.n_caixas = anot.boxes.length; }
  if (!silencioso) avisar(`salvo · ${anot.boxes.length} caixa(s)`);
}

$("anotSalvar").addEventListener("click", async () => {
  const botao = $("anotSalvar");
  botao.disabled = true;
  try {
    await salvarAnotacao();
    await irParaImagem(+1);
  } catch (e) { avisar(e.message, true); }
  finally { botao.disabled = false; }
});

/** Da tela de um dataset, ir direto rotular as imagens dele: troca de aba e
 *  já aplica o escopo, para o rotulador cair no subconjunto certo. */
async function rotularDataset(slug) {
  dados.dataset = slug;
  dados.pagina = 0;
  dados.itens = [];                 // força o (re)carregamento com o novo escopo
  preencherEscopo();
  $("escopoDataset").value = slug;
  dados.dataset = slug;
  mostrarAba("dados");              // itens vazio → carrega uma vez, já no escopo
}

// ======================================================= aba Treinamento ====
//
// Dispara um treino sobre uma versão congelada e acompanha logs + métricas ao
// vivo. A tela lê o stream incrementalmente (cursores de seq e época), então
// não relê o log inteiro a cada atualização.

const treino = { atual: null, poll: null, seq: 0, epoca: -1,
                 eventos: [], series: {} };

const RUN_ESTADO = {
  queued:    { txt: "na fila",   cls: "espera" },
  claimed:   { txt: "iniciando", cls: "ativo" },
  running:   { txt: "treinando", cls: "ativo" },
  succeeded: { txt: "concluído", cls: "ok" },
  failed:    { txt: "falhou",    cls: "ruim" },
  canceled:  { txt: "cancelado", cls: "neutro" },
};
const seloRun = (s) => {
  const e = RUN_ESTADO[s] || { txt: s, cls: "neutro" };
  return `<span class="selo-run ${e.cls}">${esc(e.txt)}</span>`;
};
const ativo = (s) => s === "queued" || s === "claimed" || s === "running";

async function carregarRuns() {
  const runs = await api(`/projects/${estado.projeto}/runs`);
  treino.runs = runs;
  $("listaRuns").innerHTML = runs.length
    ? runs.map((r) => `
        <li><button data-run="${esc(r.id)}" aria-current="${r.id === treino.atual}">
          <span class="run-linha1">${esc(r.experimento)} ${seloRun(r.job_status)}</span>
          <span class="run-linha2">${esc(r.dataset)} v${r.dataset_version}
            · ${esc(r.base_model_name || "")}
            ${r.metrics && r.metrics["mAP50-95"] != null
              ? `· mAP ${Number(r.metrics["mAP50-95"]).toFixed(3)}` : ""}</span>
        </button></li>`).join("")
    : `<li class="vazio">Nenhum treino ainda.</li>`;
  $("listaRuns").querySelectorAll("[data-run]").forEach((b) =>
    b.addEventListener("click", () => abrirRun(b.dataset.run).catch(erroAviso)));
}

function pararPollRun() {
  if (treino.poll) { clearTimeout(treino.poll); treino.poll = null; }
}

async function abrirRun(runId) {
  pararPollRun();
  treino.atual = runId;
  treino.seq = 0; treino.epoca = -1; treino.eventos = []; treino.series = {};
  document.querySelectorAll("[data-run]").forEach((b) =>
    b.setAttribute("aria-current", b.dataset.run === runId));

  const d = await api(`/projects/${estado.projeto}/runs/${runId}`);
  $("painelRun").innerHTML = `
    <div class="cabeca">
      <h1>${esc(d.experimento)}</h1>
      <span id="runSelo">${seloRun(d.job_status)}</span>
      <span class="espaco"></span>
      <button class="discreto" id="botaoCancelarRun"
        ${ativo(d.job_status) ? "" : "hidden"}>Cancelar</button>
    </div>
    <div class="run-meta">
      <span>${esc(d.dataset)} v${d.dataset_version}</span>
      <span>base ${esc(d.base_model_name || "—")}</span>
      <span>${d.params ? `${d.params.epochs} épocas · ${d.params.imgsz}px · batch ${d.params.batch}` : ""}</span>
      <span>${d.device
        ? "máquina " + esc(d.device)
        : d.target_device
          ? "alvo " + esc(d.target_device) + " · aguardando liberar"
          : "aguardando máquina"}</span>
    </div>

    <section class="secao">
      <header><span class="rotulo">Métrica de validação</span>
        <span class="espaco"></span>
        <span class="legenda"><i class="l1"></i>mAP50-95 <i class="l2"></i>mAP50</span>
      </header>
      <canvas id="runCurva" class="run-curva" width="720" height="220"></canvas>
    </section>

    <section class="secao">
      <header><span class="rotulo">Registro</span>
        <span class="espaco"></span><span id="runProgresso" class="dica"></span></header>
      <pre class="run-log" id="runLog"></pre>
    </section>

    <div id="runModelo"></div>`;

  $("botaoCancelarRun").addEventListener("click", () => cancelarRun(runId));
  await tickRun();
}

async function tickRun() {
  const runId = treino.atual;
  let s;
  try {
    s = await api(`/projects/${estado.projeto}/runs/${runId}` +
      `/stream?desde_seq=${treino.seq}&desde_epoca=${treino.epoca}`);
  } catch (e) { avisar(e.message, true); return; }
  if (treino.atual !== runId) return;      // trocou de run durante a espera

  for (const ev of s.eventos) {
    treino.eventos.push(ev);
    if (ev.seq > treino.seq) treino.seq = ev.seq;
  }
  for (const m of s.metricas) {
    (treino.series[m.key] = treino.series[m.key] || []).push(
      { epoch: m.epoch, value: m.value });
    if (m.epoch > treino.epoca) treino.epoca = m.epoch;
  }
  if (treino.eventos.length > 400)
    treino.eventos = treino.eventos.slice(-400);

  $("runSelo").innerHTML = seloRun(s.job_status);
  $("botaoCancelarRun").hidden = !ativo(s.job_status);
  const p = s.progress || {};
  $("runProgresso").textContent = p.epoch ? `época ${p.epoch}/${p.de || "?"}` : "";

  const log = $("runLog");
  const colado = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
  log.innerHTML = treino.eventos.map((e) =>
    `<span class="lvl-${esc(e.level)}">${esc(e.message)}</span>`).join("\n");
  if (colado) log.scrollTop = log.scrollHeight;

  desenharCurva($("runCurva"), treino.series);

  if (ativo(s.job_status)) {
    // só reagenda se ainda estamos NESTE run E na aba de treino: um tickRun já
    // em voo quando o usuário troca de aba resolveria o fetch e reagendaria,
    // driblando o pararPollRun (que só limpa o timeout pendente).
    if (treino.atual === runId && !$("abaTreino").hidden)
      treino.poll = setTimeout(() => tickRun().catch(() => {}), 1600);
  } else {
    // terminou: mostra o modelo produzido (se houver) e atualiza a lista
    if (s.error) avisar(s.error, true);
    const d = await api(`/projects/${estado.projeto}/runs/${runId}`);
    if (d.model_version) {
      const mv = d.model_version;
      $("runModelo").innerHTML = `
        <section class="secao"><header><span class="rotulo">Modelo produzido</span></header>
          <div class="run-modelo">
            <span class="modelo-nome">${esc(mv.model_slug)} v${mv.version}</span>
            <span>mAP50-95 ${mv.metrics && mv.metrics["mAP50-95"] != null
              ? Number(mv.metrics["mAP50-95"]).toFixed(4) : "—"}</span>
            <span class="mono uri">${esc(mv.weights_uri)}</span>
          </div></section>`;
    }
    await carregarRuns();
  }
}

/** Curva de mAP por época. Duas séries, eixo Y fixo em 0..1 (mAP é razão), X
 *  pela época. Grade a cada 0,25 para leitura sem números poluindo. */
function desenharCurva(cv, series) {
  const cx = cv.getContext("2d");
  const W = cv.width, H = cv.height, pad = 28;
  cx.clearRect(0, 0, W, H);
  const x0 = pad, x1 = W - 8, y0 = 8, y1 = H - 18;
  const px = css("--trilho") || "#33424f";
  // grade horizontal
  cx.strokeStyle = px; cx.fillStyle = css("--texto-tenue") || "#5f7386";
  cx.lineWidth = 1; cx.font = "10px ui-monospace, monospace";
  for (const g of [0, 0.25, 0.5, 0.75, 1]) {
    const y = y1 - (y1 - y0) * g;
    cx.globalAlpha = 0.5; cx.beginPath();
    cx.moveTo(x0, y); cx.lineTo(x1, y); cx.stroke(); cx.globalAlpha = 1;
    cx.fillText(g.toFixed(2), 2, y + 3);
  }
  const todas = [].concat(...Object.values(series));
  const maxEp = Math.max(1, ...todas.map((p) => p.epoch));
  const linha = (pts, cor) => {
    if (!pts || !pts.length) return;
    const ord = [...pts].sort((a, b) => a.epoch - b.epoch);
    cx.strokeStyle = cor; cx.lineWidth = 2; cx.beginPath();
    ord.forEach((p, i) => {
      const x = x0 + (x1 - x0) * (maxEp <= 1 ? 1 : (p.epoch - 1) / (maxEp - 1));
      const y = y1 - (y1 - y0) * clamp01(p.value);
      i ? cx.lineTo(x, y) : cx.moveTo(x, y);
    });
    cx.stroke();
  };
  linha(series["mAP50-95"], "#2bc47d");
  linha(series["mAP50"], "#3aa0ff");
  if (!todas.length) {
    cx.fillStyle = css("--fraco") || "#7c8b99";
    cx.fillText("aguardando a primeira época…", x0 + 8, (y0 + y1) / 2);
  }
}

const css = (v) =>
  getComputedStyle(document.documentElement).getPropertyValue(v).trim();

async function cancelarRun(runId) {
  if (!confirm("Cancelar este treino? Se já estiver rodando, a máquina para " +
               "na próxima época; o progresso até aqui não vira modelo."))
    return;
  try {
    await post(`/projects/${estado.projeto}/runs/${runId}/cancel`, {});
    avisar("Cancelamento pedido.");
    pararPollRun();          // evita dois tickRun concorrentes
    await tickRun();
  } catch (e) { avisar(e.message, true); }
}

// ---- novo treino ----

$("botaoNovoRun").addEventListener("click", () => {
  const comVersao = estado.datasets.filter((d) => d.versoes > 0);
  if (!comVersao.length) {
    avisar("congele uma versão de dataset antes de treinar", true);
    return;
  }
  $("runDataset").innerHTML = comVersao.map((d) =>
    `<option value="${esc(d.slug)}">${esc(d.name)}</option>`).join("");
  // sugere os modelos existentes; digitar um nome novo cria outro modelo
  api(`/projects/${estado.projeto}/models`).then((ms) => {
    $("listaModelosTreino").innerHTML = ms.map((m) =>
      `<option value="${esc(m.slug)}">`).join("");
  }).catch(() => {});
  // máquinas que podem treinar (fixar o job numa delas é opcional)
  api("/train-devices").then((ds) => {
    const livre = `<option value="">Próxima livre do pool</option>`;
    $("runMaquina").innerHTML = livre + ds.map((d) => {
      const off = d.status === "offline" ? " (offline)" : "";
      return `<option value="${esc(d.slug)}">${esc(d.name)}${off}</option>`;
    }).join("");
  }).catch(() => {});
  $("dlgRun").showModal();
  preencherVersoes().catch((e) => avisar(e.message, true));
});

$("runDataset").addEventListener("change",
  () => preencherVersoes().catch((e) => avisar(e.message, true)));

async function preencherVersoes() {
  const slug = $("runDataset").value;
  const d = await api(`/projects/${estado.projeto}/datasets/${slug}`);
  const sel = $("runVersao");
  sel.innerHTML = d.versoes.map((v) =>
    `<option value="${v.version}">v${v.version} — ${esc(v.descricao || "")}</option>`)
    .join("");
  const info = () => {
    const v = d.versoes.find((x) => String(x.version) === sel.value);
    $("runVersaoInfo").textContent = v
      ? `${num(v.stats.imagens)} imagens · ${num(v.stats.train)} treino · ${num(v.stats.val)} validação`
      : "";
  };
  sel.onchange = info; info();
}

$("formRun").addEventListener("submit", comTravaDeEnvio($("formRun"), async () => {
  // slugifica o nome do modelo (minúsculas/hífens) — evita pattern no input,
  // que bloquearia o submit em silêncio
  const modelo = ($("runModeloNome").value || "detector").toLowerCase()
    .normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "detector";
  try {
    const r = await post(`/projects/${estado.projeto}/runs`, {
      dataset_slug: $("runDataset").value,
      version: Number($("runVersao").value),
      model_slug: modelo,
      experiment: $("runExp").value,
      base_model: $("runBase").value,
      epochs: Number($("runEpocas").value),
      imgsz: Number($("runImg").value),
      batch: Number($("runBatch").value),
      device_slug: $("runMaquina").value || null,
    });
    $("dlgRun").close();
    avisar("Treino enfileirado.");
    await carregarRuns();
    await abrirRun(r.run_id);
  } catch (e) { avisar(e.message, true); }
}));

// ============================================================ aba Máquinas ===

async function carregarDevices() {
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

// ============================================================ aba Projeto ===
//
// Criação de projeto (com o vocabulário de classes) e adição de classe. Como o
// schema é imutável, acrescentar classe gera uma versão nova back-compatible —
// os índices antigos não mudam, então nada do que já foi rotulado é invalidado.

const RANK = { viewer: 0, annotator: 1, engineer: 2, admin: 3, owner: 4 };
const tipoProjeto = (t) =>
  ({ detect: "Detecção", segment: "Segmentação", classify: "Classificação" }[t] || t);

async function carregarProjetoTab() {
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
    pjClasses = [];
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

// ============================================================ aba Modelos ===
//
// O registro: versões imutáveis (pesos + métricas) e os apelidos que apontam
// para elas. Promover é mover o ponteiro (champion/challenger/baseline), não
// tocar na versão — dá para voltar atrás sem reconstruir nada.

const ALIASES_MODELO = ["champion", "challenger", "baseline"];
const modelos = { atual: null };

async function carregarModelos() {
  const lista = await api(`/projects/${estado.projeto}/models`);
  modelos.lista = lista;
  $("listaModelos").innerHTML = lista.length
    ? lista.map((m) => `
        <li><button data-modelo="${esc(m.slug)}" aria-current="${m.slug === modelos.atual}">
          ${esc(m.name)}
          <span class="qtd">${m.ultima ? "v" + m.ultima : "—"}</span>
        </button></li>`).join("")
    : "";
  $("listaModelos").querySelectorAll("[data-modelo]").forEach((b) =>
    b.addEventListener("click", () => abrirModelo(b.dataset.modelo).catch(erroAviso)));

  if (!lista.length) {
    modelos.atual = null;
    $("painelModelo").innerHTML = `<div class="vazio">
      <p>Nenhum modelo neste projeto ainda.</p>
      <p class="dica">Um modelo nasce de um treino concluído (aba Treinamento):
        o peso vira uma versão registrada aqui.</p></div>`;
    return;
  }
  await abrirModelo(modelos.atual || lista[0].slug);
}

async function abrirModelo(slug) {
  modelos.atual = slug;
  document.querySelectorAll("[data-modelo]").forEach((b) =>
    b.setAttribute("aria-current", b.dataset.modelo === slug));
  $("painelModelo").innerHTML = `<p class="carregando">Carregando…</p>`;
  const d = await api(`/projects/${estado.projeto}/models/${slug}`);
  const podeProm = RANK[estado.papel] >= RANK.engineer;
  // apelido vigente por versão, para marcar na tabela
  const aliasDe = {};
  d.aliases.forEach((a) => (aliasDe[a.version] = aliasDe[a.version] || []).push(a.alias));

  $("painelModelo").innerHTML = `
    <div class="cabeca">
      <h1>${esc(d.name)}</h1>
      <span class="tipo-ds">${esc(tipoProjeto(d.task_type))}</span>
      <span class="espaco"></span>
      ${d.aliases.map((a) =>
        `<span class="selo-alias ${esc(a.alias)}">${esc(a.alias)} → v${a.version}</span>`).join("")}
    </div>
    <section class="secao">
      <header><span class="rotulo">Versões</span></header>
      <table class="tab-versoes">
        <thead><tr><th>versão</th><th>mAP50-95</th><th>origem</th>
          <th>tamanho</th><th>builds</th><th>quando</th>${podeProm ? "<th>ações</th>" : ""}</tr></thead>
        <tbody>${d.versoes.map((v) => linhaModeloVersao(v, aliasDe[v.version], podeProm)).join("")}</tbody>
      </table>
    </section>
    ${seccaoComparacoes(d.versoes)}`;

  desenharComparacoes(d.versoes);

  if (podeProm) {
    $("painelModelo").querySelectorAll("[data-prom]").forEach((b) =>
      b.addEventListener("click", () =>
        promover(slug, b.dataset.alias, Number(b.dataset.prom))));
    $("painelModelo").querySelectorAll("[data-export]").forEach((b) =>
      b.addEventListener("click", () => abrirExport(slug, Number(b.dataset.export))));
    $("painelModelo").querySelectorAll("[data-deploy]").forEach((b) =>
      b.addEventListener("click", () => {
        const v = d.versoes.find((x) => x.version === Number(b.dataset.deploy));
        abrirDeploy(slug, v);
      }));
    $("painelModelo").querySelectorAll("[data-eval]").forEach((b) =>
      b.addEventListener("click", () => {
        const v = d.versoes.find((x) => x.version === Number(b.dataset.eval));
        abrirEvaluate(slug, v);
      }));
  }
  renderImplantacoes(slug).catch(() => {});
}

/** Badge de avaliação: benchmark e o mAP medido nele. Verde-selo para
 *  distinguir de build (laranja) — é métrica, não artefato. */
function badgeAval(a) {
  const m = a.metrics || {};
  const map = m["mAP50-95"] != null ? Number(m["mAP50-95"]).toFixed(3) : "—";
  return `<span class="badge-aval" title="benchmark ${esc(a.benchmark)} v${a.benchmark_version}">
    ${esc(a.benchmark)}·${map}</span>`;
}

/** Seção de implantações do projeto: device → modelo vN (formato) + estado. */
async function renderImplantacoes(slug) {
  const deps = await api(`/projects/${estado.projeto}/deployments`);
  // o usuário pode ter trocado de modelo durante o fetch: não anexa no painel
  // de outro modelo
  if (modelos.atual !== slug || !deps.length) return;
  const sec = document.createElement("section");
  sec.className = "secao";
  sec.innerHTML = `<header><span class="rotulo">Implantações</span></header>
    <ul class="lista-deploy">${deps.map((d) => `
      <li><span class="mono">${esc(d.device)}</span>
        <span>${esc(d.model_slug)} v${d.version} · ${esc(d.format)}</span>
        <span class="selo-run ${d.state === "active" ? "ok"
          : d.state === "failed" ? "ruim" : "espera"}">${esc(estadoDeploy(d.state))}</span>
        ${d.error ? `<span class="dica">${esc(d.error)}</span>` : ""}</li>`).join("")}</ul>`;
  $("painelModelo").appendChild(sec);
}

const estadoDeploy = (s) =>
  ({ pending: "aguardando", active: "ativo", failed: "falhou" }[s] || s);

async function abrirDeploy(modeloSlug, versao) {
  $("dpModelo").textContent = `${modeloSlug} v${versao.version}`;
  $("dlgDeploy").dataset.modelo = modeloSlug;
  $("dlgDeploy").dataset.version = versao.version;
  // formatos disponíveis = os builds daquela versão
  $("dpFormato").innerHTML = (versao.builds || []).map((b) =>
    `<option value="${esc(b.format)}">${esc(b.format)}${
      b.target && b.target.precision ? " · " + esc(b.target.precision) : ""}</option>`).join("");
  const devs = await api("/deploy-devices").catch(() => []);
  if (!devs.length) {
    avisar("nenhuma máquina cadastrada para receber a implantação", true);
    return;
  }
  $("dpDevice").innerHTML = devs.map((d) =>
    `<option value="${esc(d.slug)}">${esc(d.name)} (${esc(d.status)})</option>`).join("");
  $("dlgDeploy").showModal();
}

$("formDeploy").addEventListener("submit", comTravaDeEnvio($("formDeploy"), async () => {
  const slug = $("dlgDeploy").dataset.modelo;
  try {
    const r = await post(`/projects/${estado.projeto}/models/${slug}/deploy`, {
      version: Number($("dlgDeploy").dataset.version),
      format: $("dpFormato").value, device_slug: $("dpDevice").value,
    });
    $("dlgDeploy").close();
    avisar(`Implantação pedida em ${r.device}. A máquina puxa e ativa.`);
    if (modelos.atual === slug) await abrirModelo(slug).catch(() => {});
  } catch (e) { avisar(e.message, true); }
}));

/** Badge de um build: formato, precisão e a latência medida no alvo. */
function badgeBuild(b) {
  const t = b.target || {};
  const lat = b.bench && b.bench.p50_ms != null ? ` · ${b.bench.p50_ms}ms` : "";
  const rot = `${esc(b.format)}${t.precision ? " " + esc(t.precision) : ""}${
    t.dynamic ? " din" : ""}${lat}`;
  return `<span class="badge-build" title="${esc(t.device_class || "")}${
    b.built_on ? " · " + esc(b.built_on) : ""}">${rot}</span>`;
}

function linhaModeloVersao(v, aliases, podeProm) {
  const m = v.metrics || {};
  const map = m["mAP50-95"] != null ? Number(m["mAP50-95"]).toFixed(4) : "—";
  const origem = v.experimento
    ? `${esc(v.experimento)} · ${esc(v.dataset || "")} v${v.dataset_version || "?"}`
    : "—";
  const mb = v.bytes ? (v.bytes / 1e6).toFixed(1) + " MB" : "—";
  const selos = (aliases || []).map((a) =>
    `<span class="selo-alias ${esc(a)}">${esc(a)}</span>`).join(" ");
  const artefatos = [...(v.builds || []).map(badgeBuild),
                     ...(v.avaliacoes || []).map(badgeAval)].join(" ") || "—";
  const acoes = podeProm ? `<td class="prom-cel">${ALIASES_MODELO.map((a) =>
    `<button class="discreto" data-prom="${v.version}" data-alias="${a}"
       title="apontar ${a} para v${v.version}">${a[0].toUpperCase()}</button>`).join("")}
    <button class="discreto" data-eval="${v.version}" title="avaliar num benchmark">◎</button>
    <button class="discreto" data-export="${v.version}" title="compilar (onnx/engine)">⤓</button>
    ${(v.builds || []).length ? `<button class="discreto" data-deploy="${v.version}"
       title="implantar num dispositivo">↗</button>` : ""}</td>` : "";
  return `<tr>
    <td class="mono">v${v.version} ${selos}</td>
    <td class="mono">${map}</td>
    <td>${origem}</td>
    <td class="mono">${mb}</td>
    <td>${artefatos}</td>
    <td>${new Date(v.created_at).toLocaleDateString("pt-BR")}</td>
    ${acoes}</tr>`;
}

async function promover(slug, alias, version) {
  try {
    await api(`/projects/${estado.projeto}/models/${slug}/aliases/${alias}`,
              { method: "PUT", body: JSON.stringify({ version }) });
    avisar(`${alias} → v${version}`);
    await abrirModelo(slug);
    await carregarModelos();
  } catch (e) { avisar(e.message, true); }
}

// ---- pré-tagueamento em lote (a partir da Rotulagem) ----
//
// Aqui o dataset é o ESCOPO já escolhido na barra; o que se escolhe é o modelo.
// É a assistência do rotulador: os rascunhos caem na grade desta mesma aba.

/** Mostra/habilita o botão de pré-tag conforme papel e escopo. */
function atualizarBotaoPretag() {
  const b = $("botaoPreTagLote");
  b.hidden = RANK[estado.papel] < RANK.engineer;
  b.disabled = !dados.dataset;
  b.title = dados.dataset ? "rode um modelo sobre este dataset"
    : "escolha um dataset no escopo para pré-taguear";
}

$("botaoPreTagLote").addEventListener("click",
  () => abrirPreTagLote().catch(erroAviso));

async function abrirPreTagLote() {
  if (!dados.dataset) { avisar("escolha um dataset no escopo", true); return; }
  const ds = (estado.datasets || []).find((d) => d.slug === dados.dataset);
  $("plDatasetNome").textContent = ds ? ds.name : dados.dataset;
  $("dlgPrelabel").dataset.dataset = dados.dataset;
  const nPag = dados.itens.length;
  $("plEscopo").querySelector('[value="pagina"]').textContent =
    `Só esta página (${nPag} ${nPag === 1 ? "imagem" : "imagens"})`;
  $("plEscopo").value = "todo";
  const modelos = await api(`/projects/${estado.projeto}/models`).catch(() => []);
  if (!modelos.length) {
    avisar("nenhum modelo treinado ainda — treine um na aba Treinamento", true);
    return;
  }
  $("plModeloSel").innerHTML = modelos.map((m) => {
    const c = m.aliases && m.aliases.champion;
    return `<option value="${esc(m.slug)}">${esc(m.name)}${c ? " · champion v" + c : ""}</option>`;
  }).join("");
  await preencherPretagVersoes();
  $("dlgPrelabel").showModal();
}

$("plModeloSel").addEventListener("change",
  () => preencherPretagVersoes().catch((e) => avisar(e.message, true)));

async function preencherPretagVersoes() {
  const slug = $("plModeloSel").value;
  const d = await api(`/projects/${estado.projeto}/models/${slug}`).catch(() => null);
  const champ = d && (d.aliases.find((a) => a.alias === "champion") || {}).version;
  $("plVersao").innerHTML =
    `<option value="">champion${champ ? " (v" + champ + ")" : " — nenhum, usa a última"}</option>` +
    (d ? d.versoes.map((v) => `<option value="${v.version}">v${v.version}</option>`).join("") : "");
}

$("formPrelabel").addEventListener("submit", comTravaDeEnvio($("formPrelabel"), async () => {
  const versao = $("plVersao").value;
  const corpo = {
    model_slug: $("plModeloSel").value,
    conf: Number($("plConf").value), only_missing: $("plMissing").checked,
  };
  if ($("plEscopo").value === "pagina") {
    corpo.sample_ids = dados.itens.map((it) => it.id);
    if (!corpo.sample_ids.length) { avisar("esta página está vazia", true); return; }
  } else {
    corpo.dataset_slug = $("dlgPrelabel").dataset.dataset;
  }
  if (versao) corpo.version = Number(versao);
  try {
    const r = await post(`/projects/${estado.projeto}/prelabel`, corpo);
    $("dlgPrelabel").close();
    avisar(`Pré-tag enfileirado (modelo v${r.model_version}). Aguardando uma máquina…`);
    acompanharPrelabel(r.job_id);
  } catch (e) { avisar(e.message, true); }
}));

/** Acompanha o job de pré-tag até terminar, avisando o progresso. Diferente do
 *  treino (que tem tela própria), aqui basta o toast — o resultado aparece na
 *  aba Rotulagem como rascunho. */
async function acompanharPrelabel(jobId) {
  const url = `/projects/${estado.projeto}/jobs/${jobId}`;
  for (let i = 0; i < 1200; i++) {
    let j;
    try { j = await api(url); } catch { return; }
    if (j.status === "succeeded") {
      const r = j.result || {};
      avisar(`Pré-tag pronto: ${num(r.pretags)} caixas em ${num(r.imagens)} imagens. ` +
             `Veja na aba Rotulagem.`);
      return;
    }
    if (j.status === "failed") { avisar(j.error || "pré-tag falhou", true); return; }
    if (j.status === "canceled") { avisar("pré-tag cancelado"); return; }
    if (j.progress && j.progress.feito)
      avisar(`Pré-tagueando… ${num(j.progress.feito)}/${num(j.progress.de)}`);
    await new Promise((r) => setTimeout(r, 1500));
  }
}

// ---- export / compilação (a partir da aba Modelos) ----

function abrirExport(modeloSlug, version) {
  $("exModelo").textContent = `${modeloSlug} v${version}`;
  $("dlgExport").dataset.modelo = modeloSlug;
  $("dlgExport").dataset.version = version;
  alternarEngineOpts();
  $("dlgExport").showModal();
}

function alternarEngineOpts() {
  // opções de engine (precisão/DLA/dinâmico) só fazem sentido para engine
  $("exEngineOpts").style.display = $("exFormato").value === "engine" ? "" : "none";
}
$("exFormato").addEventListener("change", alternarEngineOpts);

// DLA compila com shape fixo; fora do DLA, dinâmico é o recomendado (igual ao .pt)
$("exDla").addEventListener("change", () => {
  $("exDyn").disabled = $("exDla").checked;
  $("exDyn").checked = !$("exDla").checked;
});

$("formExport").addEventListener("submit", comTravaDeEnvio($("formExport"), async () => {
  const slug = $("dlgExport").dataset.modelo;
  const version = Number($("dlgExport").dataset.version);
  const corpo = { format: $("exFormato").value, imgsz: Number($("exImg").value) };
  if (corpo.format === "engine") {
    corpo.precision = $("exPrec").value;
    corpo.dla = $("exDla").checked;
    corpo.dynamic = $("exDyn").checked;
  }
  try {
    const r = await post(
      `/projects/${estado.projeto}/models/${slug}/versions/${version}/export`, corpo);
    $("dlgExport").close();
    avisar(`Compilação enfileirada (${corpo.format}). Aguardando uma máquina…`);
    acompanharExport(r.job_id, slug);
  } catch (e) { avisar(e.message, true); }
}));

/** Acompanha o job de export; ao terminar, recarrega o modelo para o build
 *  aparecer na versão. */
async function acompanharExport(jobId, modeloSlug) {
  const url = `/projects/${estado.projeto}/jobs/${jobId}`;
  for (let i = 0; i < 1200; i++) {
    let j;
    try { j = await api(url); } catch { return; }
    if (j.status === "succeeded") {
      const r = j.result || {};
      const lat = r.bench && r.bench.p50_ms != null ? ` · ${r.bench.p50_ms}ms` : "";
      avisar(`Build pronto (${r.format}${lat}).`);
      if (modelos.atual === modeloSlug && !$("abaModelos").hidden)
        await abrirModelo(modeloSlug).catch(() => {});
      return;
    }
    if (j.status === "failed") { avisar(j.error || "compilação falhou", true); return; }
    if (j.status === "canceled") { avisar("compilação cancelada"); return; }
    await new Promise((r) => setTimeout(r, 1500));
  }
}

// ---- avaliação (a partir da aba Modelos) ----

async function abrirEvaluate(modeloSlug, versao) {
  $("avModelo").textContent = `${modeloSlug} v${versao.version}`;
  $("dlgEvaluate").dataset.modelo = modeloSlug;
  $("dlgEvaluate").dataset.version = versao.version;
  // datasets com versão congelada servem de benchmark
  const comVersao = (estado.datasets || []).filter((d) => d.versoes > 0);
  if (!comVersao.length) {
    avisar("congele uma versão de dataset para usar como benchmark", true);
    return;
  }
  // artefatos a medir: peso + cada build compilado, e o "todos" para comparar
  const builds = versao.builds || [];
  const opts = [`<option value="todos">Todos os formatos (comparar)</option>`,
                `<option value="pt;;">peso (.pt)</option>`];
  for (const b of builds) {
    const t = b.target || {};
    const rot = b.format + (t.precision ? " " + t.precision : "") +
      (t.dynamic ? " din" : "") + (t.dla ? " DLA" : "");
    opts.push(`<option value="${esc(b.format)};${esc(t.precision || "")};${t.dla ? "1" : ""}">${esc(rot)}</option>`);
  }
  $("avArtefato").innerHTML = opts.join("");
  $("avDataset").innerHTML = comVersao.map((d) =>
    `<option value="${esc(d.slug)}">${esc(d.name)}</option>`).join("");
  $("dlgEvaluate").showModal();
  await preencherBenchVersoes().catch((e) => avisar(e.message, true));
}

/** Descreve os artefatos a avaliar a partir da opção escolhida. */
function artefatosEscolhidos() {
  const v = $("avArtefato").value;
  const parse = (s) => {
    const [format, precision, dla] = s.split(";");
    const o = { format };
    if (precision) o.precision = precision;
    if (dla) o.dla = true;
    return o;
  };
  if (v !== "todos") return [parse(v)];
  return [...$("avArtefato").options].filter((o) => o.value !== "todos")
    .map((o) => parse(o.value));
}

$("avDataset").addEventListener("change",
  () => preencherBenchVersoes().catch((e) => avisar(e.message, true)));

async function preencherBenchVersoes() {
  const slug = $("avDataset").value;
  if (!slug) { $("avVersao").innerHTML = ""; return; }
  const d = await api(`/projects/${estado.projeto}/datasets/${slug}`);
  const sel = $("avVersao");
  sel.innerHTML = d.versoes.map((v) =>
    `<option value="${v.version}">v${v.version} — ${esc(v.descricao || "")}</option>`).join("");
  const info = () => {
    const v = d.versoes.find((x) => String(x.version) === sel.value);
    $("avInfo").textContent = v ? `${num(v.stats.imagens)} imagens no benchmark` : "";
  };
  sel.onchange = info; info();
}

$("formEvaluate").addEventListener("submit", comTravaDeEnvio($("formEvaluate"), async () => {
  const slug = $("dlgEvaluate").dataset.modelo;
  const version = $("dlgEvaluate").dataset.version;
  const base = { dataset_slug: $("avDataset").value,
                 dataset_version: Number($("avVersao").value) };
  const artefatos = artefatosEscolhidos();
  try {
    const jobs = [];
    for (const art of artefatos) {
      const r = await post(
        `/projects/${estado.projeto}/models/${slug}/versions/${version}/evaluate`,
        { ...base, ...art });
      jobs.push(r.job_id);
    }
    $("dlgEvaluate").close();
    avisar(artefatos.length > 1
      ? `${artefatos.length} avaliações enfileiradas (comparação). Aguardando máquina…`
      : "Avaliação enfileirada. Aguardando uma máquina…");
    // acompanha todas; cada uma recarrega o modelo ao concluir
    jobs.forEach((j) => acompanharAval(j, slug));
  } catch (e) { avisar(e.message, true); }
}));

async function acompanharAval(jobId, modeloSlug) {
  const url = `/projects/${estado.projeto}/jobs/${jobId}`;
  for (let i = 0; i < 1200; i++) {
    let j;
    try { j = await api(url); } catch { return; }
    if (j.status === "succeeded") {
      const m = (j.result || {}).metrics || {};
      avisar(`Avaliação pronta: mAP50-95 ${m["mAP50-95"] != null ? Number(m["mAP50-95"]).toFixed(4) : "—"}.`);
      if (modelos.atual === modeloSlug && !$("abaModelos").hidden)
        await abrirModelo(modeloSlug).catch(() => {});
      return;
    }
    if (j.status === "failed") { avisar(j.error || "avaliação falhou", true); return; }
    if (j.status === "canceled") { avisar("avaliação cancelada"); return; }
    await new Promise((r) => setTimeout(r, 1500));
  }
}

// ---- comparação de avaliações (pt × engine × DLA: mAP + pré/inf/pós) ----

function rotuloAval(a) {
  const t = a.target || {};
  return (a.format || "pt") + (t.precision ? " " + t.precision : "") +
         (t.dynamic ? " din" : "") + (t.dla ? " DLA" : "");
}

/** Agrupa as avaliações por (versão, benchmark) — cada grupo é uma comparação
 *  entre artefatos (pt/onnx/engine/DLA) no mesmo conjunto. */
function gruposAval(versoes) {
  const grupos = [];
  for (const v of versoes || []) {
    const porBench = {};
    for (const a of (v.avaliacoes || [])) {
      const k = `${a.benchmark}@v${a.benchmark_version}`;
      (porBench[k] = porBench[k] || []).push(a);
    }
    Object.entries(porBench).forEach(([k, avs], i) => grupos.push({
      id: `cmp-${v.version}-${i}`, titulo: `v${v.version} · benchmark ${k}`,
      rows: avs.map((a) => {
        const m = a.metrics || {};
        const pre = m.pre_ms || 0, inf = m.inf_ms || 0, pos = m.pos_ms || 0;
        return { rotulo: rotuloAval(a), map: m["mAP50-95"], map50: m["mAP50"],
                 pre, inf, pos, total: pre + inf + pos };
      }),
    }));
  }
  return grupos;
}

function seccaoComparacoes(versoes) {
  const grupos = gruposAval(versoes);
  if (!grupos.length) return "";
  return `<section class="secao">
    <header><span class="rotulo">Comparação de avaliações</span>
      <span class="espaco"></span>
      <span class="legenda"><i style="background:#3aa0ff"></i>pré
        <i style="background:#ff8a1f"></i>inferência
        <i style="background:#2bc47d"></i>pós</span></header>
    ${grupos.map((g) => `<div class="cmp-grupo">
      <span class="cmp-titulo">${esc(g.titulo)}</span>
      <table class="tab-versoes cmp-tab"><thead><tr><th>artefato</th>
        <th>mAP50-95</th><th>mAP50</th><th>pré</th><th>inf</th><th>pós</th>
        <th>total</th></tr></thead>
        <tbody>${g.rows.map(linhaAval).join("")}</tbody></table>
      <canvas class="cmp-chart" id="${g.id}" width="640"
        height="${Math.max(60, g.rows.length * 34 + 16)}"></canvas>
    </div>`).join("")}</section>`;
}

function linhaAval(r) {
  const ms = (x) => x ? x.toFixed(1) : "—";
  const mp = (x) => x != null ? Number(x).toFixed(3) : "—";
  return `<tr><td class="mono">${esc(r.rotulo)}</td><td class="mono">${mp(r.map)}</td>
    <td class="mono">${mp(r.map50)}</td><td class="mono">${ms(r.pre)}</td>
    <td class="mono">${ms(r.inf)}</td><td class="mono">${ms(r.pos)}</td>
    <td class="mono">${r.total ? r.total.toFixed(1) + "ms" : "—"}</td></tr>`;
}

function desenharComparacoes(versoes) {
  for (const g of gruposAval(versoes)) {
    const cv = document.getElementById(g.id);
    if (cv) desenharBarras(cv, g.rows);
  }
}

/** Barras horizontais empilhadas: pré + inferência + pós por artefato, largura
 *  ∝ ms. É o "onde o tempo é gasto" — DLA/engine encurtam a inferência. */
function desenharBarras(cv, rows) {
  const cx = cv.getContext("2d");
  cx.clearRect(0, 0, cv.width, cv.height);
  const padL = 96, padR = 66, top = 6, h = 22, gap = 12;
  const maxTotal = Math.max(0.001, ...rows.map((r) => r.total));
  const escala = (cv.width - padL - padR) / maxTotal;
  const cores = ["#3aa0ff", "#ff8a1f", "#2bc47d"];
  cx.font = "11px ui-monospace, monospace";
  rows.forEach((r, i) => {
    const y = top + i * (h + gap);
    cx.fillStyle = css("--texto-fraco") || "#8fa3b3";
    cx.textAlign = "right"; cx.fillText(r.rotulo, padL - 6, y + h / 2 + 4);
    let x = padL;
    [r.pre, r.inf, r.pos].forEach((seg, k) => {
      const w = seg * escala;
      cx.fillStyle = cores[k]; cx.fillRect(x, y, Math.max(0, w), h); x += w;
    });
    cx.fillStyle = css("--texto") || "#e6edf3"; cx.textAlign = "left";
    cx.fillText(r.total ? r.total.toFixed(1) + "ms" : "sem tempo", x + 6, y + h / 2 + 4);
  });
}

// ============================================================ aba Usuários ===
//
// Administração da instalação (superusuário). Sem serviço de e-mail, o convite
// e a redefinição saem como token para repassar por canal seguro.

const US_ESTADO = { active: "ativo", invited: "convidado", disabled: "desativado" };

async function carregarUsuarios() {
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
