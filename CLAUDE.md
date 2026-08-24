# visionyard — guia para trabalhar neste repositório

O que o sistema faz, como subir e como configurar está no [`README.md`](README.md)
e no [`agent/README.md`](agent/README.md). Aqui ficam só as convenções e as
armadilhas de quem vai *editar* o código.

Plano de trabalho corrente: [`.claude/PLANO.md`](.claude/PLANO.md).

## Comandos

```bash
.venv/bin/uvicorn api.main:app --reload --port 8000   # subir a API
.venv/bin/python -m pytest                            # suíte (precisa de Postgres)
.venv/bin/python -m db.migrate --status               # o que está pendente
.venv/bin/ruff check .                                # lint (config no pyproject)
.venv/bin/mypy                                        # tipos, sobre api/ e db/
```

O CI (`.github/workflows/ci.yml`) roda exatamente esses três últimos. O
ferramental fica em `requirements-dev.txt`, com versão fixada para que o CI e a
máquina de quem programa reprovem as mesmas coisas.

Os testes criam e derrubam `visionyard_test` a cada sessão. Nunca apontam para o
banco de desenvolvimento: `tests/conftest.py` fixa o DSN **antes** de importar
`api.config`, porque as settings são lidas no import.

## Layout

```
api/            Control API (FastAPI)
  main.py       montagem dos routers e do estático
  config.py     settings por ambiente (prefixo VISIONYARD_)
  db.py         pool psycopg3; SQL direto, sem ORM
  storage.py    porta do GCS: cliente memoizado e URL assinada
  logs.py       código de correlação por requisição, e a rede do 500
  security.py   hash de senha e tokens
  auth.py       login, sessão, token de API, token de device
  audit.py      audit(), fora de deps.py para auth.py poder chamá-la
  deps.py       principal da requisição, RBAC por projeto
  routers/      uma rota por arquivo de aba — só HTTP
  services/     a regra de domínio e o SQL que a implementa
  importers/    adaptadores de origem (GCS, Label Studio) sobre base.Importer
  static/       módulos ES servidos direto, sem build (app.js é a entrada)
agent/          worker das máquinas de execução
contracts/      o que API e agente combinam (payload de job); só stdlib
db/migrations/  SQL versionado, NNN_nome.sql
tests/          suíte contra Postgres real
```

## Convenções

**Router é HTTP, serviço é regra.** O arquivo em `routers/` declara a rota, o
modelo pydantic do corpo, o RBAC e o código de status; quem decide *o que
acontece* — e escreve o SQL que faz acontecer — mora no módulo irmão em
`services/`. Regra nova vai para o serviço mesmo que só uma rota a use, para não
voltar a ser preciso adivinhar onde ela está.

Duas escolhas dentro dessa divisão, feitas para não criar cerimônia: o serviço
**levanta `HTTPException`** (uma hierarquia paralela de erros de domínio seria
uma segunda descrição do mesmo erro) e o serviço **não conhece pydantic** —
recebe escalares e `dict`/`list[dict]`, e é o router que valida e converte.

`agents`, `models`, `samples` e `training` já estão assim: o router não tem uma
linha de SQL. `datasets` está pela metade — `services/datasets.py` tem os
auxiliares do congelamento (resolve → split → manifesto), mas a regra de
versionamento e as 21 consultas seguem no `freeze` de `routers/datasets.py`.
`imports` também: `services/imports.py` tem o ciclo de vida do job (ponto,
progresso, desfecho, recolhimento), e o resto da rota — cerca de caminho,
upsert de origem, listagem — segue inline. Os demais (`auth`, `buckets`,
`devices`, `jobs`, `prelabel`, `projects`, `users`) carregam o SQL inline por
inteiro. Quando um deles for mexido a sério, a regra vai junto para
`services/`.

**Toda rota declara o que devolve.** O `response_model` do router é o contrato
de saída; sem ele a linha do banco *é* o DTO, e coluna acrescentada a um
`SELECT` viaja para o navegador sem ninguém decidir.
`tests/test_contrato_resposta.py` varre o OpenAPI e reprova rota que não
declarou. A única exceção é a porta do agente (`/v1/agent/*`): o que ela
devolve é o payload do job, cuja descrição única é `contracts/jobs.py`.

Modelo novo se escreve **coluna a coluna a partir do `SELECT`** — não há build
nem lint no front, então chave que some da resposta só aparece quando alguém
abre a tela.

**SQL direto, sem ORM.** O schema é escrito à mão em `db/migrations/`; um ORM por
cima seria uma segunda descrição do mesmo modelo para manter em sincronia. Query
nova vai no módulo que a usa, com `%s`/`%(nome)s` — nunca f-string com valor.

**Payload de job é contrato, não dicionário.** O que a API grava em
`job.payload` e o que o executor do agente lê é uma dataclass em
`contracts/jobs.py` — o produtor constrói e grava `como_dict()`, o consumidor
lê com `de_payload()` e usa atributo. É a única borda do sistema que atravessa
máquina e cadência de deploy, e antes do contrato os dois lados a descreviam
por conta própria: renomear uma chave passava no lint, no tipo e na suíte, e
falhava horas depois como `KeyError` num dispositivo. `contracts/` não importa
de `api` nem de `agent` e não usa nada além da stdlib — é o que permite ao
dispositivo de borda carregá-lo sem levar fastapi e psycopg junto.

**Chave de payload não se renomeia.** Job enfileirado antes de um deploy é
reclamado depois dele: mudar um nome em `contracts/jobs.py` quebra o que já
está na fila. Campo novo entra com default; renomear ou remover é
`PAYLOAD_VERSION` nova, e o parser recusa versão que não conhece com uma
mensagem que manda atualizar o agente. Chave desconhecida é ignorada de
propósito — o controlador sobe antes dos agentes.

**Migration aplicada não se edita.** O runner guarda checksum e recusa arquivo
alterado. Mudança de schema é sempre um `NNN_` novo.

**Comentário explica o porquê, não o quê.** É o padrão dominante no repositório e
vale mantê-lo: quando um trecho existe para fechar uma corrida, um vazamento ou
uma regressão, o comentário diz qual. Ver o bloco sobre o lock consultivo em
`routers/datasets.py` (`freeze`) e o `assign_splits` de `services/datasets.py`
como referência de tom.

**Idioma.** Domínio, tabelas, colunas e nomes de rota em inglês
(`dataset_version`, `manifest_uri`, `samples_created`). Variável local, mensagem
de erro, docstring e comentário em português. Chave de resposta JSON segue o que
a interface já consome — há mistura consolidada (`gravadas`, `alvos`, `stats`);
o nome mora no `response_model` do router, e renomeá-lo lá é mexer no `app.js`
junto. Parâmetro de query segue a regra da chave de resposta, e não a do nome
de rota: `pagina`, `filtro` e `limite` convivem com `limit` e `type` porque são
superfície que o `app.js` consome.

Duas exceções que ficam: `008_indices_e_imutabilidade.sql` e
`009_sample_source_e_cascade.sql` têm nome em português. Renomear arquivo de
migration é pior do que a inconsistência — o nome é a chave em
`schema_migrations`, e trocá-lo faria o runner reaplicar a migration.

**Erro de usuário é HTTPException com texto em português.** Erro de infra
(GCS, banco) vai para o log e devolve mensagem genérica — o texto do
fornecedor cita conta de serviço e caminho interno, e `job.error` é lido com
papel `viewer`.

O que nenhum handler previu cai na rede de `logs.correlacionar`: vira 500 com
o código da requisição no corpo e o traceback no log, sob o mesmo código. Toda
linha de log emitida durante uma requisição carrega esse código sem que quem
chama o logger precise fazer nada — o filtro está no root. Ao investigar, é
por ele que se procura; ao responder a um usuário que viu um 500, é ele que se
pede.

## Invariantes que não podem quebrar

- **Versão é imutável.** `dataset_version`, `model_version`, `evaluation` e
  `model_build` só recebem INSERT (a migration 008 impõe). Recongelar cria linha
  nova; nunca faça UPDATE nessas tabelas.
- **Anotação é por (imagem, autor).** Escrita mexe só na do próprio autor.
  Importação e pré-tag nunca sobrescrevem trabalho humano — o `WHERE` do
  `DO UPDATE` é o que garante, não a ordem das operações.
- **RBAC é por projeto.** Rota com `{project_slug}` usa
  `Depends(require_role("papel"))`. Não-membro recebe 404, não 403: 403
  revelaria que o projeto existe. A dependência **não** garante que o projeto
  existe — o superusuário passa por ela sem consulta nenhuma; o handler que
  precisa da linha usa `deps.projeto_ou_404`.
- **Relato atrasado do agente não reescreve desfecho.** A rede pisca e o
  `agent/client.py` repete o POST; `finalizar_job` e `atualizar_deployment` só
  transicionam a partir de estado não-terminal (`status <> ALL(TERMINAIS)`,
  `state = 'pending'`). Sem isso um treino cancelado volta como concluído e uma
  implantação superseded ressuscita por cima da que está valendo.
- **Agente e usuário são portas diferentes.** Device autentica por
  `X-Agent-Token` e nunca por `Authorization`. Rota de agente vive em
  `routers/agents.py` e a regra em `services/agents.py`. **Toda** operação sobre
  um job confere que ele é daquela máquina, por um de dois caminhos: a maioria
  chama `job_da_maquina` (ou `job_com_payload`, que a chama);
  `atualizar_progresso`, `finalizar_job` e `atualizar_deployment` fazem a
  conferência no próprio `WHERE` (`AND device_id = %s`). Nesses três o `WHERE`
  **é** o controle de acesso — não o simplifique achando que `job_da_maquina`
  já cobriu.
- **Rota de agente que lê payload confere o tipo do job.** `job_com_payload`
  recusa com 404 antes de ler, e é ela quem barra um agente chamando `/build`
  num job de treino. Antes o que barrava era a chave faltando no payload — um
  404 por acidente, e em `/targets` nem isso: o escopo lido com `.get()` virava
  `None` e a resposta era 200 com lista vazia. Payload do tipo certo que o
  parser recusa é bug de quem enfileirou: sobe e vira 500 na rede de
  `logs.correlacionar`, não 404. A exceção é `/manifest`, que tem dois donos —
  o treino aprende com o manifesto e a avaliação mede contra ele, e os dois
  payloads carregam `dataset_version_id`. Ela confere contra os dois tipos
  (`_confere_tipo`), e ter dois donos não é ter todos: um `export` continua
  levando 404.
- **Job que roda dentro da API não é reclamável por máquina nenhuma.** O
  claim casa `capabilities @> requires` — e `requires` vazio casa com qualquer
  device —, não filtra por tipo, e realcança job `running` com ponto velho.
  Quem não tem executor do outro lado fica de fora por
  `services/agents.py: TIPOS_LOCAIS`, no claim E no reaper. Sem isso a máquina
  encerra a importação como "tipo não suportado" por cima de quem a está
  executando, e ganha sempre: o prazo do agente (180s) é menor que o dela
  (900s).
- **O agente não declara capacidade de roteamento.** `registrar_presenca` filtra
  por `CAPS_DIAGNOSTICO`; deixar `train`/`infer` passar permite que um
  dispositivo de borda reclame treino.
- **Escrita vinda do agente é escopada pelo projeto do job.** O `sample_id` vem
  do corpo da requisição — o `SELECT ... WHERE s.project_id = %s` dentro do
  INSERT (`services/agents.py: gravar_prelabels`) é o que impede injetar dado em
  outro projeto.
- **Split é sorteado no congelamento, uma vez.** Sortear no treino torna dois
  runs incomparáveis.

## Armadilhas conhecidas

- `db.fetch_one/fetch_all/execute` pegam **uma conexão cada** quando chamados
  sem `conn=`. Sequência que precisa ser atômica vai em
  `async with db.connection()` + `conn.transaction()`, e todo helper de dentro
  recebe `conn=conn` — inclusive o `audit()`, para que o fato e o seu registro
  comitem juntos.
- Job de importação roda em `asyncio.create_task` dentro do processo da API: a
  task morre com o processo, e nenhuma máquina o reclama (ver a invariante
  acima). Quem cuida dele é
  `services/imports.py: recolher_orfaos`, chamada nas duas telas de jobs — a
  importação bate ponto a cada lote e quem parou de bater há
  `import_job_stale_seconds` é fechada como falha. Toda escrita de desfecho
  daquele job (`concluir_job`, `falhar_job`) leva `AND status = 'running'` no
  `WHERE`: sem isso a task que sobreviveu ao recolhimento reescreveria como
  `succeeded` um job que a tela já deu por interrompido.
- O front monta HTML por template string. Todo valor vindo do servidor passa por
  `esc()`. Não há build nem lint: o descuido não é detectado.
- **O front é módulo ES nativo, sem build nem bundler.** `app.js` é a entrada
  (`<script type="module">`) e importa os demais; `nucleo.js` é a raiz do grafo
  e não importa de ninguém — é o que garante que o `$` exista quando cada
  módulo liga os seus listeners ao ser avaliado. Há ciclos entre os módulos de
  tela (o anotador chama `mostrarAba`, as abas chamam o anotador) e eles são
  inofensivos porque nenhuma chamada cruzada acontece no topo do módulo; pôr
  uma ali quebra o carregamento com um `ReferenceError` de TDZ.
- **Módulo ES não deixa um módulo atribuir à variável importada de outro.** Ler
  é permitido, escrever não. Estado compartilhado que precisa mudar é objeto
  (`estado`, `dados`, `anot`) e muda por propriedade; onde havia um `let`
  (`pjClasses`), quem o zera é uma função exportada (`esquecerClasses`).
- **A casca (`GET /`) vai com `Cache-Control: no-cache`.** Ela diz qual é o
  módulo de entrada: uma casca velha em cache pedindo `app.js` como script
  comum quebra a página inteira com "Cannot use import statement outside a
  module" — aconteceu na própria migração para módulos.
- `db.fetch_one` devolve `dict | None`, e o `mypy` não deixa mais indexar isso
  direto. Query que **sempre** devolve linha (`INSERT ... RETURNING`, `count(*)`)
  usa `db.fetch_row`, que estoura na própria query se vier vazia. Onde a linha
  pode faltar, é `fetch_one` + 404 — e num handler de rota com `{project_slug}`
  o 404 do projeto vem de `deps.projeto_ou_404`: `require_role` deixa o
  superusuário passar sem conferir se o projeto existe.
  Dentro de transação, prefira `db.fetch_row(..., conn=conn)` a
  `conn.execute(...).fetchone()`: o `conn` é tipado (`db.Conn`), então as duas
  formas são checadas, mas só a primeira já traz a mensagem com o SQL à vista.
