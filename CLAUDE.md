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
  security.py   hash de senha e tokens
  auth.py       login, sessão, token de API, token de device
  audit.py      audit(), fora de deps.py para auth.py poder chamá-la
  deps.py       principal da requisição, RBAC por projeto
  routers/      uma rota por arquivo de aba — só HTTP
  services/     a regra de domínio e o SQL que a implementa
  importers/    adaptadores de origem (GCS, Label Studio) sobre base.Importer
  static/       interface web servida direto, sem build
agent/          worker das máquinas de execução
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

`agents`, `datasets`, `models` e `training` já estão assim. Os routers menores
(`auth`, `buckets`, `devices`, `imports`, `jobs`, `prelabel`, `projects`,
`samples`, `users`) ainda carregam o SQL inline; quando um deles for mexido a
sério, a regra vai junto para `services/`.

**SQL direto, sem ORM.** O schema é escrito à mão em `db/migrations/`; um ORM por
cima seria uma segunda descrição do mesmo modelo para manter em sincronia. Query
nova vai no módulo que a usa, com `%s`/`%(nome)s` — nunca f-string com valor.

**Migration aplicada não se edita.** O runner guarda checksum e recusa arquivo
alterado. Mudança de schema é sempre um `NNN_` novo.

**Comentário explica o porquê, não o quê.** É o padrão dominante no repositório e
vale mantê-lo: quando um trecho existe para fechar uma corrida, um vazamento ou
uma regressão, o comentário diz qual. Ver `routers/datasets.py:299` e
`services/datasets.py:65` como referência de tom.

**Idioma.** Domínio, tabelas, colunas e nomes de rota em inglês
(`dataset_version`, `manifest_uri`, `samples_created`). Variável local, mensagem
de erro, docstring e comentário em português. Chave de resposta JSON segue o que
a interface já consome — há mistura consolidada (`gravadas`, `alvos`, `stats`);
não renomeie sem mexer no `app.js`.

**Erro de usuário é HTTPException com texto em português.** Erro de infra
(GCS, banco) vai para o log e devolve mensagem genérica — o texto do
fornecedor cita conta de serviço e caminho interno, e `job.error` é lido com
papel `viewer`.

## Invariantes que não podem quebrar

- **Versão é imutável.** `dataset_version`, `model_version`, `evaluation` e
  `model_build` só recebem INSERT (a migration 008 impõe). Recongelar cria linha
  nova; nunca faça UPDATE nessas tabelas.
- **Anotação é por (imagem, autor).** Escrita mexe só na do próprio autor.
  Importação e pré-tag nunca sobrescrevem trabalho humano — o `WHERE` do
  `DO UPDATE` é o que garante, não a ordem das operações.
- **RBAC é por projeto.** Rota com `{project_slug}` usa
  `Depends(require_role("papel"))`. Não-membro recebe 404, não 403: 403
  revelaria que o projeto existe.
- **Agente e usuário são portas diferentes.** Device autentica por
  `X-Agent-Token` e nunca por `Authorization`. Rota de agente vive em
  `routers/agents.py`, e toda operação passa por `services/agents.py:
  job_da_maquina`, que valida que o job é daquela máquina.
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
- Job de importação roda em `asyncio.create_task` dentro do processo da API e
  fica preso em `running` se o processo reiniciar — o reaper de `routers/agents.py`
  não o alcança, porque filtra por `heartbeat_at`, que em job de importação é NULL.
- O front monta HTML por template string. Todo valor vindo do servidor passa por
  `esc()`. Não há build nem lint: o descuido não é detectado.
- `db.fetch_one` devolve `dict | None` e boa parte do código indexa o resultado
  direto. O `mypy` roda com `index`/`dict-item` desligados por causa disso (ver
  PLANO, item 9) — enquanto estiverem desligados, é revisão humana que separa
  "query que sempre devolve linha" de "falta um 404 aqui".
