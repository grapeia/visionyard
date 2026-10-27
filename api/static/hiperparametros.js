/* Editor separado: campos vazios mantêm o padrão da biblioteca. O catálogo e
 * a validação vêm da API, também usados pelo executor do treino. */
import { api, post, esc } from "./nucleo.js";

let dialogo;

export async function editarHiperparametros(projeto, atuais, aplicar) {
  const opcoes = await api(`/projects/${projeto}/training-options`);
  if (dialogo) dialogo.remove();
  dialogo = document.createElement("dialog");
  dialogo.id = "dlgHiperparametros";
  dialogo.style.cssText = "width:min(900px,94vw);max-height:90vh;overflow:auto";
  dialogo.innerHTML = `<form method="dialog">
    <h2>Hiperparâmetros do treino</h2>
    <p class="ajuda">Configure só o que quiser mudar. Campo vazio usa o padrão da
      Ultralytics instalada na máquina. A configuração fica guardada neste treino.</p>
    <div class="acoes" style="flex-wrap:wrap">
      <button type="button" data-acao="dev">Usar configuração do dev</button>
      <button type="button" data-acao="padrao">Restaurar padrões</button>
      <button type="button" data-acao="exportar">Baixar configuração</button>
    </div>
    <details><summary>Colar configuração (JSON ou dicionário Python)</summary>
      <p class="dica">Pode colar a lista completa. Campos de outras tarefas ou
        gerenciados pelo sistema serão listados antes de aplicar.</p>
      <textarea id="hpTexto" rows="8" style="width:100%;font-family:monospace"
        placeholder='{"patience":150,"optimizer":"Adam","mosaic":0}'></textarea>
      <button type="button" data-acao="importar">Carregar texto nos campos</button>
    </details>
    <div id="hpAviso" role="status" style="white-space:pre-wrap"></div>
    <div id="hpCampos"></div>
    <p class="dica">Dataset, modelo-base, nomes de saída e salvamento dos pesos
      são controlados pelo sistema. GPU local não é a máquina escolhida no pool.</p>
    <div class="acoes">
      <button type="button" data-acao="cancelar">Cancelar</button>
      <button class="primario" value="ok">Aplicar ao treino</button>
    </div>
  </form>`;
  document.body.append(dialogo);
  const dlg = dialogo, form = dlg.querySelector("form");
  const aviso = dlg.querySelector("#hpAviso");
  let valores = { ...atuais }, ocupado = false;
  const mensagem = (texto) => { aviso.textContent = texto; };

  function desenhar() {
    const grupos = [...new Set(opcoes.fields.map((c) => c.group))];
    dlg.querySelector("#hpCampos").innerHTML = grupos.map((grupo, i) =>
      `<details ${i === 0 ? "open" : ""} style="margin:16px 0">
        <summary>${esc(grupo)}</summary>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px">
        ${opcoes.fields.filter((c) => c.group === grupo).map(campo).join("")}
        </div></details>`).join("");
  }
  function campo(c) {
    const v = valores[c.name];
    const id = `hp_${c.name}`;
    let input;
    let escolhas = c.type === "bool" ? [true, false]
      : c.type === "cache" ? [false, true, "ram", "disk"] : c.choices;
    if (escolhas) {
      if (c.nullable) escolhas = [null, ...escolhas];
      input = `<select id="${esc(id)}" data-param="${esc(c.name)}">
        <option value="">Padrão da biblioteca</option>
        ${escolhas.map((x) => `<option value="${esc(JSON.stringify(x))}"
          ${v !== undefined && JSON.stringify(v) === JSON.stringify(x) ? "selected" : ""}>
          ${esc(x === true ? "Sim" : x === false ? "Não" : x === null ? "Nenhum" : x)}</option>`).join("")}
        </select>`;
    } else {
      const numerico = c.type === "int" || c.type === "float";
      const texto = v == null ? "" : Array.isArray(v) ? JSON.stringify(v) : String(v);
      input = `<input id="${esc(id)}" data-param="${esc(c.name)}"
        type="${numerico ? "number" : "text"}" value="${esc(texto)}"
        ${numerico ? `min="${c.min}" max="${c.max}" step="${c.name === "imgsz" ? "32" : c.type === "int" ? "1" : "any"}"` : ""}
        placeholder="${c.type === "freeze" ? "Padrão; ou 10 / [0,1,2]" : c.type === "device" ? "Automático; ou 0 / cpu" : "Padrão da biblioteca"}">`;
    }
    return `<div><label for="${esc(id)}">${esc(c.name)}</label>${input}
      <p class="dica">${esc(c.description)}</p></div>`;
  }
  function ler() {
    const saida = {};
    for (const c of opcoes.fields) {
      const texto = dlg.querySelector(`#hp_${c.name}`).value.trim();
      if (texto === "") continue;
      if (c.type === "device") saida[c.name] = texto;
      else if (c.type === "int" || c.type === "float") saida[c.name] = Number(texto);
      else saida[c.name] = JSON.parse(texto);
    }
    return saida;
  }
  async function validar(params) {
    const r = await post(`/projects/${projeto}/training-options/parse`, {
      text: JSON.stringify(params),
    });
    if (r.ignored.length) throw new Error(r.ignored.map((x) => `${x.name}: ${x.reason}`).join("\n"));
    return r.params;
  }
  async function executar(acao) {
    if (ocupado) return;
    ocupado = true;
    dlg.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try { await acao(); }
    catch (e) { mensagem(e.message || "Configuração inválida."); }
    finally {
      ocupado = false;
      dlg.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    }
  }
  dlg.querySelector('[data-acao="dev"]').onclick = () => {
    valores = { ...opcoes.preset_dev }; desenhar();
    mensagem("Configuração do dev carregada. Confira batch 80 e GPU 0 para a máquina escolhida.\nEla só entra no formulário quando você clicar em Aplicar ao treino.");
  };
  dlg.querySelector('[data-acao="padrao"]').onclick = () => {
    valores = { ...opcoes.defaults }; desenhar(); mensagem("Padrões restaurados no editor.");
  };
  dlg.querySelector('[data-acao="cancelar"]').onclick = () => dlg.close();
  dlg.querySelector('[data-acao="importar"]').onclick = () => executar(async () => {
    const r = await post(`/projects/${projeto}/training-options/parse`, {
      text: dlg.querySelector("#hpTexto").value,
    });
    // Importar substitui os avançados, conservando os básicos se não vierem.
    const anteriores = Object.fromEntries(["epochs", "imgsz", "batch"].map((k) =>
      [k, Number(dlg.querySelector(`#hp_${k}`).value) || opcoes.defaults[k]]));
    valores = { ...Object.fromEntries(["epochs", "imgsz", "batch"]
      .filter((k) => k in anteriores).map((k) => [k, anteriores[k]])), ...r.params };
    desenhar();
    mensagem(r.ignored.length ? "Campos não aplicados:\n" + r.ignored
      .map((x) => `${x.name}: ${x.reason}`).join("\n") : "Configuração carregada nos campos.");
  });
  dlg.querySelector('[data-acao="exportar"]').onclick = () => executar(async () => {
    if (!form.reportValidity()) return;
    const params = await validar(ler());
    const url = URL.createObjectURL(new Blob([JSON.stringify(params, null, 2)], {type:"application/json"}));
    const a = document.createElement("a"); a.href = url; a.download = "hiperparametros.json";
    a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    mensagem("Configuração exportada para reutilizar em outro treino.");
  });
  form.onsubmit = (ev) => {
    ev.preventDefault();
    executar(async () => {
      const params = await validar(ler());
      if (!dlg.open) return;
      aplicar(params); dlg.close("ok");
    });
  };
  desenhar(); dlg.showModal();
}
