# Plano — qualidade de arquitetura

Documento vivo. Cada item tem estado (`pendente` / `em curso` / `feito`) e é
atualizado à medida que o trabalho anda. O registro do que mudou fica no fim.

**Origem:** revisão de arquitetura em 2026-08-10. O veredito foi que a lógica de
domínio está sólida (imutabilidade, concorrência, segurança) e que o que falta é
ferramental e separação de camadas.

A intenção era que nenhum item mudasse comportamento do produto. Não se
sustentou: o review do item 4 desenterrou dois defeitos reais (um introduzido
pela própria refatoração, outro pré-existente) e corrigi-los mudou
comportamento. O item 10 tem mais três esperando. Vale como intenção — mexer em
camada não é desculpa para mexer em regra —, não como promessa.

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
da transação; `audit()` não faz mais `import json` no corpo (morava em
`deps.py`; o item 4 a moveu para `api/audit.py`).
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

`datasets` ficou pela metade, de propósito: os auxiliares do congelamento estão
no serviço, mas a regra de versionamento (`routers/datasets.py:268`, `freeze`)
não foi mexida — ela tem lock consultivo, ordem de escrita e publicação de
manifesto entrelaçados, e movê-la junto teria dobrado o tamanho do diff. Os
demais routers (`auth`, `buckets`, `devices`, `imports`, `jobs`, `prelabel`,
`projects`, `samples`, `users`) seguem com SQL inline por inteiro. Não é mais
ambiguidade — a regra está escrita; é dívida a pagar quando cada um for mexido a
sério. `imports.py` é o mais provável de puxar cedo, pelo item 5.

O review do item 4 achou dois defeitos que **foram corrigidos aqui** e três que
viraram o item 10:

- **Regressão introduzida:** com a auditoria dentro da transação de
  `accept_invite`, um `X-Forwarded-For` que não é endereço (o `unknown` do
  nginx) estourava o cast para `inet` e passava a derrubar a ativação da conta
  junto — antes só se perdia o registro. `client_ip` agora devolve endereço
  válido ou nada, caindo no peer da conexão.
- **Bug pré-existente, corrigido por ser barato:** `finalizar_job` casava só por
  (id, device_id). Como o reaper deixa o job terminal mas mantém o `device_id`,
  o agente que voltava de uma partição ressuscitava como `succeeded` um treino
  que o usuário tinha cancelado.
- As docstrings dos handlers foram restauradas: ao mudar o corpo para o serviço,
  17 rotas ficaram sem `description` no OpenAPI (o FastAPI lê a do handler, não
  a do serviço). Conferido rota a rota — as mesmas 41 rotas sem descrição antes
  e depois.

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
- `dataset.selection` (jsonb, 002) está morta: a 007 trocou o modelo de "filtro
  por metadados" por origens explícitas (`dataset_source`), e nenhuma linha de
  Python ou de JS lê ou escreve a coluna. O comentário dela na migration ainda
  descreve o modelo antigo em detalhe — era o que o `README.md` repetia até
  2026-08-11. Derrubar exige `NNN_` novo; enquanto não vier, é a próxima
  armadilha de quem for ler o schema.

### 9. Ligar `index` e `dict-item` no `mypy` — `feito` (2026-08-11)
Os dois códigos estão ligados; os 55 lugares apontados viraram duas coisas
distintas, que era o ponto do item:

- **`db.fetch_row`** para query que sempre devolve linha (`INSERT ...
  RETURNING`, `count(*)`, agregado sem `GROUP BY`) — estoura na query, com o SQL
  à vista, em vez do `TypeError` obscuro três linhas adiante. 35 chamadas.
- **`deps.projeto_ou_404`** para o 404 que faltava. `require_role` não garante
  que o projeto existe: `project_role` devolve `owner` ao superusuário sem
  consultar nada, então slug inexistente virava 500 no primeiro `proj["id"]`.
  Passaram a responder 404 o `GET`/`PATCH` de projeto, `GET /samples`,
  `POST /datasets` e as duas rotas de membro. Os dois handlers que já faziam a
  checagem à mão (`prelabel`, `append_classes`) usam o mesmo helper.

Mudou comportamento (500 → 404), e é o que o teste parametrizado em
`test_projects.py` fixa. `db/migrate.py` passou a medir duração em Python: as
duas consultas a `clock_timestamp()` existiam só para isso.

A primeira leva cobriu só `db.fetch_one`: `db.connection()` não tinha anotação
de retorno, então o `conn` do `async with` saía `Any` e todo
`(await conn.execute(...).fetchone())["x"]` — metade das leituras, e justamente
a que roda dentro de transação — seguia invisível ao `mypy`. Fechado com
`db.Conn = AsyncConnection[dict[str, Any]]` no pool e nos helpers, mais os 24
sites que a anotação revelou.

### 10. Achados do review do item 4 — `feito` (2026-08-11)
Três bugs pré-existentes que o item 4 só mudou de arquivo. Todos corrigidos.

- **`build_manifest` não tratava `md5` nulo** (`services/datasets.py`).
  `bytes(it["md5"]).hex()` estourava `TypeError` quando a coluna é NULL, e a 008
  tornou `sample.md5` anulável (a própria migration manda o operador zerar o
  placeholder da 006). O `freeze` só captura `ValueError`, então virava 500 e o
  dataset não congelava mais. Agora recusa com 409 dizendo quais imagens travam
  — incluí-las não é opção, o md5 é a chave de cache e o nome do arquivo que o
  agente materializa. Instalação nova não passa por isso.
- **`atualizar_deployment` não olhava o estado atual** (`services/agents.py`).
  Só transiciona quem está em `pending`; reenviar o estado já registrado é 200
  (o cliente do agente repete de propósito em erro de rede), qualquer outra
  transição é 409. Sem isso, o `active` reenviado ressuscitava a implantação
  superseded E supersedia a que estava valendo, e o `failed` de fallback do
  `main.py` derrubava a que o servidor já tinha ativado. A serialização é
  `pg_advisory_xact_lock` pelo **device**: o `SELECT ... FOR UPDATE` que usei
  primeiro travava só a linha relatada e deixava duas ativações simultâneas de
  implantações diferentes colidirem no índice único.
- **`fetch_one` indexado sem checar `None`** em `reclamar_job`,
  `registrar_build` e `deployments_pendentes` — resolvido no item 9 com
  `_device_do_token`: device apagado no meio de um `/claim` dá 401, não 500.

Uma armadilha de teste, também descoberta no review: `tests/conftest.py` fixa o
nome `visionyard_test` e derruba/recria o banco por sessão. **Duas suítes
simultâneas se atropelam** (`DeadlockDetected` nas migrations, depois
`AdminShutdown`). Se algum dia rodar suíte em paralelo, o nome do banco tem de
sair de uma variável por processo.

### 11. Achados do review dos itens 9 e 10 — `em curso`
Sobrou o que o review de 2026-08-11 confirmou e que não cabia nas correções. Os
dois bugs pré-existentes estão corrigidos; o primeiro item continua aberto
porque é decisão de modelo, não conserto.

- **Dataset com imagem sem md5 não tem saída fina.** O congelamento recusa (e
  agora avisa antes, no `preview`), mas a única forma de destravar é tirar do
  dataset a origem inteira daquelas imagens — leva junto todas as outras da
  mesma origem. Reimportar não serve: o dedup é por md5 e a linha nula nunca
  casa, então nasce uma imagem nova e a anotação humana fica presa à antiga.
  `sample` é imutável por trigger, então não há backfill por UPDATE.
  Saídas possíveis: manifesto v1.1 com identidade de conteúdo caindo no
  `sha256` (que essas linhas têm — o placeholder da 006 nasceu dele), ou uma
  rota de manutenção que troque `annotation.sample_id` para a linha reimportada
  e apague a velha. Nenhuma urgente: só afeta instalação que passou pela 006.
- ~~**O total da grade de imagens ignora o filtro**~~ — `feito` (`da7e21f`). O
  `count(*)` ganhou o mesmo LEFT JOIN com `annotation` e o mesmo `{cond}` da
  consulta dos itens; sem o join, `{cond}` não teria de onde falar de `a`.
- ~~**`sample_id`/`sample_ids` do prelabel não são validados**~~ — `feito`
  (`3efde0f`). Tipados como `UUID` no modelo pydantic, como já era em
  `routers/jobs.py:43`. Id malformado é 422 (erro de entrada), não o 404 que o
  plano supunha: 404 diria "não existe" sobre algo que nunca foi um id. O
  payload do job segue guardando string, que é o que o agente lê.

Os dois eram pré-existentes — nenhum dos commits desta semana os tocou.

### 12. Achados do review do item 11 — `pendente`
O que o review de 2026-08-11 confirmou e ficou de fora da leva de correções.
Tudo em `routers/samples.py`, fora o último — cabe numa passada só, quando o
arquivo for mexido a sério (é candidato a virar `services/samples.py`).

- **A contagem e a página leem snapshots diferentes.** `fetch_all` e
  `fetch_row` pegam uma conexão cada, então uma anotação gravada entre as duas
  idas ao banco reabre o descompasso que o `da7e21f` fechou. Cuidado: passar
  `conn=conn` às duas **não** resolve — sob READ COMMITTED cada comando ainda
  tira o próprio snapshot. Fechar de verdade pede transação REPEATABLE READ,
  cara para uma contagem de tela; a alternativa é a tela assumir a folga.
- **O `count` junta `annotation` mesmo com `filtro=todas`**, que é o padrão da
  aba: o join só existe para o `{cond}` ter de onde falar de `a`, e com `cond`
  vazio é trabalho jogado fora em toda carga de página.
- **O esqueleto `FROM ... WHERE` está escrito duas vezes** (itens e contagem) —
  é a duplicação que produziu o bug original. Montar o rabo comum uma vez e
  interpolar nas duas torna a divergência impossível em vez de remendada.
- **`test_prelabel_id_malformado_nao_e_500` roda `_produzir_modelo` à toa**: o
  422 sai da validação do pydantic antes de qualquer linha do handler, e o
  `admin` é superusuário, então nada daquele setup é lido.

### 13. Contrato do payload de job — `feito` (2026-08-20)
O sistema tem duas APIs e só uma é defendida. A borda HTTP tem pydantic; a
borda que atravessa processo, máquina e cadência de deploy — o controlador
atualiza, o agente na borda não — é `json.dumps(dict)` de um lado
(`routers/prelabel.py:109`, `services/training.py`) e `payload["weights_uri"]`
do outro (`agent/prelabel.py:42`, `agent/train.py:27`, `agent/export.py:56`).
Sem campo de versão, sem esquema, e sem um teste que afirme que o job
enfileirado pela API é um job que o agente sabe ler.

Renomear uma chave passa no `ruff`, no `mypy` e nos 169 testes, e falha horas
depois como `KeyError` num dispositivo — marcando o job como `failed` com uma
mensagem que não diz que o contrato quebrou. Agrava: `agent/` só é exercitado
por `test_agent_materialize.py` e `test_export.py`.

`contracts/jobs.py` é a descrição única dos quatro tipos que vão para o agente
(`train`, `prelabel`, `export`, `evaluate`): o produtor constrói a dataclass e
grava `como_dict()`, o executor lê com `de_payload()` e usa atributo.

**Dataclass e não pydantic**, ao contrário do que este item supunha: o agente
não tem pydantic hoje, e cada dependência dele é uma roda a achar para a
arquitetura certa (aarch64 do JetPack). `contracts/` também não importa de
`api` nem de `agent` — importar `api` levaria fastapi e psycopg para a borda.
Como o produtor já valida a entrada com pydantic na rota, o que faltava do
outro lado era parse com erro legível, e isso a stdlib dá.

**A v1 é exatamente o que já estava gravado.** Job enfileirado antes do commit
é reclamado depois dele, então nenhuma chave foi renomeada para entrar no
contrato — inclusive `rotulo`, em português no meio de chaves em inglês. O
parser trata as duas assimetrias do deploy: chave desconhecida é ignorada (o
controlador sobe antes dos agentes) e `v` maior que o conhecido é recusado
mandando atualizar o agente.

O `mypy` passou a cobrir `contracts/` e `agent/` — é o que torna o contrato
exigível, porque só o lado que LÊ reprovando é que impede o `p.weights_url`
de chegar ao dispositivo. Quatro erros de tipo pré-existentes do agente saíram
no caminho (os quatro executores importados sob o mesmo nome `executar`,
`proc.stdout` opcional, o dict-célula `melhor`, o rebind de `im` no recorte).

Dois efeitos colaterais, ambos simplificação: o `run_id` do treino agora é
sorteado em Python antes do INSERT do job, o que dispensou o
`UPDATE ... payload || jsonb` que existia só porque o run tem FK para o job; e
o `payload.get("artifact_uri") or payload.get("weights_uri")` do `evaluate`
era fallback morto — nenhum payload de avaliação carrega `weights_uri`.

Ficou de fora, de propósito:

- **O terceiro leitor.** `services/agents.py` lê o payload cru em seis lugares
  (`alvos_prelabel`, `gravar_prelabels`, `diretorio_manifesto`,
  `registrar_modelo`, `registrar_build`, `registrar_avaliacao`), e é onde o
  default `"detector"` do `model_slug` está duplicado com o do produtor.
  Converter exige decidir o que fazer com os 404 de payload incompleto que
  existem lá (`job sem model_version`): com o parser eles viram erro de
  programação, e um agente que chama `/build` num job de treino passaria a
  receber 500 em vez de 404.
- **`params` do treino continua `dict`.** Os defaults de `epochs`/`imgsz`/
  `batch` seguem duplicados entre o `RunIn` da rota e o executor. Não há
  `KeyError` a fechar ali (o executor já lê com `.get(chave, default)`), que é
  o risco que este item existia para tirar; virar dataclass aninhada é v2.
- **O job `import`.** Roda dentro do processo da API e não passa pelo agente;
  entra em `POR_TIPO` quando o item 5 o tornar reclamável.

### 14. Contrato de resposta — `pendente`
Três rotas usam `response_model` (as três em `routers/auth.py`); as demais
devolvem a linha do banco montada à mão — `{**r, "url": ...}` em
`samples.py:137` é o padrão. A linha **é** o DTO: coluna acrescentada a um
`SELECT` viaja para o navegador sem ninguém decidir, e o OpenAPI não descreve
saída nenhuma.

É também a causa raiz da frase do `CLAUDE.md` sobre chave de JSON — "não
renomeie sem mexer no `app.js`". Não há camada onde renomear: o nome da coluna,
o nome no contrato e o nome que o front lê são a mesma string. Não precisa ser
tudo de uma vez; começar pelas rotas que o `app.js` consome com força já cria o
lugar.

### 15. Conexão como parâmetro do serviço — `pendente`
O `CLAUDE.md` explica que cada `fetch_*` pega uma conexão própria e que
sequência atômica pede `db.connection()` + `conn.transaction()`. É convenção
conferida por quem revisa: nada no tipo impede um serviço de fazer
leia-decida-escreva com três conexões diferentes. O primeiro item do 12 (a
contagem e a página em snapshots distintos) é uma instância da classe, não um
caso isolado.

A resposta estrutural é o serviço **receber** `conn: db.Conn` em vez de alcançar
o pool: com a conexão como parâmetro, esquecer a transação vira erro de tipo em
vez de achado de review. Cabe fazer serviço a serviço, junto com a migração de
cada router para `services/` — não é uma passada só.

### 16. Porta de storage — `pendente`
O acesso ao GCS está espalhado e sem porta. `routers/samples.py:63` faz
`from .buckets import _client` — um router entrando no helper privado de outro,
o único import cruzado entre routers da árvore. Quatro lugares constroem
`storage.Client()` (`samples.py:61`, `buckets.py:37,54`,
`importers/gcs_prefix.py:41,57`), e o de `samples` constrói um por requisição,
dentro do threadpool, a cada carga de página da galeria.
`importers/base.Importer` é a única porta de verdade que existe hoje.

Um `api/storage.py` com cliente memoizado e `assinar`/`listar`/`baixar` colapsa
os quatro, mata o import cruzado e cria o lugar onde um dublê de teste caberia
— hoje URL assinada não tem como ser exercitada sem credencial real: a suíte
passa por `_assinar` em toda listagem, mas só pelo caminho em que ele desiste e
devolve vazio, e nenhum teste afirma nada sobre isso.

## Registro

- **2026-08-10** — revisão de arquitetura, criação do `CLAUDE.md` e deste plano.
- **2026-08-10** — itens 1, 2 e 3 feitos. Ferramental (ruff/mypy/CI), 28
  `Annotated[object]` → `Principal`, `conn=` opcional em `db.py`/`audit()` com
  as auditorias de escrita dentro da transação. Item 9 criado a partir do que o
  `mypy` encontrou. Suíte: 147 testes passando (eram 144).
- **2026-08-11** — item 4 feito (`f828a3f`). `api/services/` com `agents`,
  `models` e `training` convertidos por inteiro e `datasets` pela metade; os
  três routers correspondentes ficaram só com HTTP. `audit()` virou
  `api/audit.py`, o que fechou a pendência de auditoria do `/invite/accept`
  deixada pelo item 3.
- **2026-08-11** — review do item 4 (alto esforço, 9 achados de 20 candidatos).
  Quatro commits de correção: `d3df6b8` `client_ip` só devolve endereço — era
  regressão da própria refatoração, porque a auditoria dentro da transação
  passou a derrubar o aceite de convite quando o `X-Forwarded-For` não é IP;
  `a62f0d5` guarda de estado terminal no `finish`, bug pré-existente que fazia
  treino cancelado reaparecer como concluído; `1350cad` docstrings dos handlers
  de volta, porque 17 rotas tinham perdido a `description` do OpenAPI; `27e3020`
  três afirmações do `CLAUDE.md` que a refatoração tornou falsas. Item 10 criado
  com os três achados pré-existentes que ficaram de fora.
  Suíte: **151 testes passando** (eram 147 no início do dia). Os quatro novos
  cobrem a auditoria do convite (na transação, e nenhum registro quando
  recusado), o `X-Forwarded-For` que não é endereço e o `finish` atrasado que
  não pode ressuscitar job encerrado.
- **2026-08-11** — itens 10 e 9 feitos. Os dois bugs do item 10 corrigidos com
  teste antes (`59f140f` relato de deployment só sai de `pending`, `b92b42d`
  congelar recusa imagem sem md5); o terceiro caiu junto com o item 9
  (`9e87e26`), que ligou `index`/`dict-item` no `mypy` e separou os 55 sites em
  `db.fetch_row` (query que sempre devolve linha) e `deps.projeto_ou_404` (404
  que faltava para superusuário). Suíte: **162 testes passando** (eram 151).
- **2026-08-11** — review dos quatro commits acima (alto esforço, 25 agentes, 16
  achados verificados de 22 candidatos). Quatro commits de correção: `cce999f`
  404 no `archive` de projeto inexistente (rota esquecida pela varredura do item
  9); `826a3a8` a mensagem do md5 mandava reimportar, o que cria duplicata em vez
  de consertar — e o `preview` agora avisa antes de a tela oferecer o botão;
  `a8c84bb` o lock do relato de deployment passou a ser pelo device, porque o
  `FOR UPDATE` por linha deixava duas ativações simultâneas colidirem no índice
  único; `1c03d1e` `db.connection()` anotado, que era o que faltava para o item 9
  valer para as leituras dentro de transação. Item 11 criado com o que sobrou.
  Suíte: **165 testes passando**.
- **2026-08-11** — os dois bugs pré-existentes do item 11 corrigidos, com teste
  antes: `da7e21f` o total da grade passou a contar o que a grade mostra, e
  `3efde0f` os ids do prelabel viraram `UUID` no pydantic. Sobrou do item 11 só
  o dataset com imagem sem md5, que é escolha de modelo (manifesto v1.1 com
  identidade no `sha256` ou rota de manutenção que remaneje a anotação) e não
  conserto. Suíte: **167 testes passando**.
- **2026-08-11** — review dos dois commits acima (alto esforço, 15 agentes, 7
  achados verificados de 12 candidatos, 2 refutados). Os três de correção
  confirmados foram corrigidos com teste antes: `9e1ab06` juntou os dois de
  `samples.py` — o `sample_id` de rota que ia cru para coluna uuid (o mesmo
  buraco que o `3efde0f` fechara no prelabel, deixado aberto uma função abaixo)
  e o `origin` que caía de 'correction' para 'manual' no segundo save, corroendo
  justamente a métrica que o módulo existe para medir; `bc69c18` apelido
  inexistente virou 404 no prelabel, em vez de rodar calado a última versão.
  Item 12 criado com os quatro restantes. Suíte: **169 testes passando**.
- **2026-08-20** — itens 13 a 16 criados. Não vieram de review de commit: são o
  que uma leitura do repositório inteiro mostrou e que a revisão de 2026-08-10
  não nomeou. Dois deles são contratos ausentes (o payload de job entre API e
  agente, a resposta HTTP), um é disciplina de transação que hoje só existe como
  convenção escrita, e o último é o acesso ao GCS sem porta. Ordem sugerida: 13
  primeiro — maior risco pelo menor esforço, e encosta no item 5 —, depois 16,
  depois 6. O 15 anda junto com a migração de cada router para `services/`, e o
  12 entra quando `samples.py` virar serviço.
- **2026-08-20** — item 13 feito (`06925f5`). `contracts/jobs.py` com os quatro
  payloads que vão para o agente, produtores e executores ligados nele, e o
  `mypy` estendido a `contracts/` e `agent/` — que é o que torna o contrato
  exigível, ao custo de quatro erros de tipo pré-existentes do agente
  corrigidos no caminho. Os oito testes novos são a resposta à pergunta que o
  item fazia: quatro enfileiram pela rota de verdade e leem o payload com o
  parser do executor (renomear uma chave no serviço agora reprova — conferido
  quebrando de propósito), quatro fixam o comportamento do parser diante de
  chave faltando, versão futura, chave desconhecida e payload sem `v`. Ficaram
  registrados no item os três recortes deixados de fora. Suíte: **177 testes
  passando** (eram 169).
