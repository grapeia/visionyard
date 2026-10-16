/* Aba Treinamento: dispara um treino sobre uma versão congelada e acompanha
 * logs e métricas ao vivo.
 *
 * A tela lê o stream incrementalmente (cursores de seq e época), então não relê
 * o log inteiro a cada atualização. */

import { $, api, avisar, clamp01, comTravaDeEnvio, cor, css, erroAviso, esc, estado, num, post } from "./nucleo.js";

const BASES = [["yolo11n", "YOLO11n — mais rápido"], ["yolo11s", "YOLO11s"],
               ["yolo11m", "YOLO11m — mais preciso"]];

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

export async function carregarRuns() {
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

export function pararPollRun() {
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
  // O peso-base acompanha a tarefa: a API recusa a combinação errada, e
  // oferecer só a certa evita o 422 depois de preencher o formulário.
  const sufixo = estado.tarefa === "segment" ? "-seg" : "";
  $("runBase").innerHTML = BASES.map(([b, rotulo]) =>
    `<option value="${b}${sufixo}.pt" ${b === "yolo11n" ? "selected" : ""}>` +
    `${rotulo}${sufixo ? " (segmentação)" : ""}</option>`).join("");
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
