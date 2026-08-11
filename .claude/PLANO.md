# Plano — qualidade de arquitetura

Documento vivo. Cada item tem estado (`pendente` / `em curso` / `feito`) e é
atualizado à medida que o trabalho anda. O registro do que mudou fica no fim.

**Origem:** revisão de arquitetura em 2026-08-10. O veredito foi que a lógica de
domínio está sólida (imutabilidade, concorrência, segurança) e que o que falta é
ferramental e separação de camadas. Nenhum item abaixo muda comportamento do
produto — são todos sobre não deixar regressão passar despercebida.

## Fila

### 1. Ferramental: lint, tipo e CI — `feito` (2026-08-10)
`pyproject.toml` com `ruff` e `mypy` (e a config do pytest, que saiu do
`pytest.ini`), `requirements-dev.txt` com as versões fixadas e
`.github/workflows/ci.yml` rodando `ruff check` / `mypy` / `pytest` contra um
Postgres de serviço.

Duas decisões que ficaram registradas na config:

- **`ruff format` não foi adotado.** Rodá-lo reescreveria ~3.800 linhas em 51
  arquivos: o repositório usa continuação alinhada à mão (inclusive no SQL
  concatenado) e o formatador troca tudo por um argumento por linha. Fica como
  escolha em aberto — se for adotado, o melhor momento é agora, com histórico
  curto, e o CI ganha um `ruff format --check`.
- **`SIM117`, `SIM105` e `RUF001-003` desligados**, com o motivo escrito no
  `pyproject.toml`: os dois primeiros brigam com idiomas do repositório, o
  terceiro reclama de travessão e × em texto português.

### 2. `Annotated[object, ...]` → `Principal` — `feito` (2026-08-10)
28 ocorrências trocadas em `api/routers/`. O `mypy` agora trava recaída.

### 3. `db.py` aceitar conexão externa — `feito` (2026-08-10)
`fetch_one/fetch_all/execute` e `audit()` aceitam `conn=` opcional (o helper
`_usar` cai no pool quando não recebe nada), e `auth.revoke_all_sessions`
também. As 24 escritas de rota que auditavam por fora passaram a auditar dentro
da transação; `deps.audit()` não faz mais `import json` no corpo.
`tests/test_auditoria.py` cobre o mecanismo: transação abortada não deixa
registro, transação comitada deixa, e quem não passa `conn` continua pegando
conexão do pool.

Duas ficaram de fora, de propósito:

- `POST /invite/accept` — a escrita acontece dentro de `auth.accept_invite`, que
  tem transação própria; incluir a auditoria exigiria a função devolver a
  conexão. Fica com o item 4 (camada de serviço). **Fechado no item 4.**
- A troca de senha agora é atômica (senha + sessões + tokens + auditoria), que
  era o caso em que a divisão doía de verdade.

### 4. Camada de serviço, escolher um lado — `feito` (2026-08-11)
O lado escolhido foi `api/services/`: router declara rota, pydantic, RBAC e
código de status; o serviço decide o que acontece e escreve o SQL. A convenção
está no `CLAUDE.md`, com as duas decisões que a tornam barata — o serviço
levanta `HTTPException` (hierarquia paralela de erro seria uma segunda descrição
do mesmo erro) e não conhece pydantic (recebe escalares e `dict`).

O que mudou:

- `api/datasets.py` → `api/services/datasets.py`, resolvendo a colisão de nome
  com `routers/datasets.py`.
- `routers/agents.py`: 625 → 250 linhas, com `services/agents.py` levando o
  protocolo inteiro (reaper, claim, `job_da_maquina`, prelabel, model, build,
  evaluation, deployment, finish). De brinde sumiram quatro
  `SELECT created_by FROM job` redundantes: `job_da_maquina` já traz a coluna.
- `routers/models.py` (389 → 133) e `routers/training.py` (262 → 82), com
  `services/models.py` e `services/training.py`.
- `audit()` saiu de `deps.py` para `api/audit.py` — não é dependência do
  FastAPI, e de dentro de `deps` o `auth.py` não conseguia importá-la sem ciclo.
  Com ela fora, `auth.accept_invite` passou a auditar dentro da própria
  transação, fechando a pendência do item 3. `api/audit.py` importa `Principal`
  só sob `TYPE_CHECKING`, que é o que mantém o ciclo aberto em tempo de
  execução.

Os routers menores (`auth`, `buckets`, `devices`, `imports`, `jobs`, `prelabel`,
`projects`, `samples`, `users`) seguem com SQL inline. Não é mais ambiguidade —
a regra está escrita; é dívida a pagar quando cada um for mexido a sério.
`imports.py` é o mais provável de puxar cedo, pelo item 5.

### 5. Importação sair do processo da API — `pendente`
`imports.py:175` usa `asyncio.create_task`. Três consequências: deploy no meio
deixa job em `running` para sempre (o reaper filtra por `heartbeat_at`, NULL
nesse caso); o hash de arquivo grande disputa o event loop com quem está
atendendo requisição; e não sobrevive a mais de uma réplica.

Já existe fila com claim/heartbeat para o agente. Caminho: importação vira job
reclamável pela mesma infraestrutura. Alternativa barata enquanto isso: reaper
que fecha job de importação sem heartbeat com idade maior que N.

### 6. Quebrar o `app.js` — `pendente`
2.771 linhas, sem módulos, estado global mutável (`estado`, `anot`, `treino`,
`dados`, `roiEdit`) e zero teste. O anotador (canvas, alças, teclado) é a parte
mais propensa a bug do sistema e a menos protegida.

ES modules nativos resolvem sem build (`<script type="module">`). Ordem sugerida:
`rede.js` / `anotador.js` / `datasets.js` / `treino.js`, começando pelo anotador.

### 7. Observabilidade — `pendente`
Sem handler global de exceção, sem log estruturado, sem request-id. Só
`imports.py` e `buckets.py` usam `logging`, e não há configuração — a saída
depende do default do uvicorn. Um 500 hoje não se correlaciona com o request que
o causou.

### 8. Menores — `pendente`
- `/healthz` bate no banco: oscilação do Postgres vira loop de restart no
  orquestrador. Separar liveness de readiness.
- Convenção de idioma PT/EN agora está escrita no `CLAUDE.md`; conferir se o
  código diverge dela em algum ponto e alinhar.

### 9. Ligar `index` e `dict-item` no `mypy` — `pendente`
Nasceu do item 1: com esses dois códigos ligados, o `mypy` aponta ~60 lugares
que indexam o retorno de `db.fetch_one` sem checar `None`. São dois casos
distintos misturados, e é a separação que dá valor:

- query que **sempre** devolve linha (`INSERT ... RETURNING`, `count(*)`) —
  pede um helper que estoure explicitamente se vier `None`, em vez do
  `TypeError` obscuro três linhas adiante;
- **404 que falta** — tipicamente `proj["id"]` logo depois de `require_role`:
  para superusuário, que passa sem o projeto existir, hoje vira 500. O padrão
  correto já está em `routers/prelabel.py` e `routers/projects.py`
  (`if proj is None: raise HTTPException(404, ...)`).

Enquanto não for feito, os dois códigos ficam em `disable_error_code` no
`pyproject.toml`.

## Registro

- **2026-08-10** — revisão de arquitetura, criação do `CLAUDE.md` e deste plano.
- **2026-08-10** — itens 1, 2 e 3 feitos. Ferramental (ruff/mypy/CI), 28
  `Annotated[object]` → `Principal`, `conn=` opcional em `db.py`/`audit()` com
  as auditorias de escrita dentro da transação. Item 9 criado a partir do que o
  `mypy` encontrou. Suíte: 147 testes passando (eram 144).
- **2026-08-11** — item 4 feito. `api/services/` com `agents`, `datasets`,
  `models` e `training`; os três routers correspondentes ficaram só com HTTP.
  `audit()` virou `api/audit.py`, o que fechou a pendência de auditoria do
  `/invite/accept` deixada pelo item 3. Suíte: 149 testes passando (eram 147) —
  os dois novos cobrem que aceitar convite audita na mesma transação e que
  convite recusado não deixa registro.
