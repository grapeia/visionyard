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

### 5. Importação sair do processo da API — `feito` (2026-08-24)
`imports.py` usava `asyncio.create_task`. Três consequências: deploy no meio
deixava job em `running` para sempre; o hash de arquivo grande disputa o event
loop com quem está atendendo requisição; e não sobrevive a mais de uma réplica.

**A primeira foi fechada em `dfc39bb`**, pela alternativa barata que este item
já previa: a importação bate ponto a cada lote e `recolher_orfaos` fecha como
falha quem parou de bater. Isso continua valendo — é o que fecha a importação
que começou e morreu no meio.

**As outras duas saíram com a fila** (`api/worker.py`). O POST enfileira
`queued` e responde; quem executa reclama com `FOR UPDATE SKIP LOCKED` e só aí
marca `running`. O executor é o mesmo código em dois lugares: dentro do
processo da API por default (`import_worker_embedded`, para a instalação de uma
máquina não precisar subir nada a mais) ou como `python -m api.worker`. Com o
laço fora, o hash para de disputar o event loop; com o claim atômico, duas
réplicas deixam de ser problema e passam a ser capacidade.

A decisão de modelo que o item supunha resolvida e não estava: **o agente não
serve de executor de importação**. O import do Label Studio lê o dump e as
imagens sob `import_root`, no disco da API; uma máquina de GPU ou de borda não
enxerga esse caminho, e mandá-la enxergar seria expor o diretório de importação
na rede. Por isso a importação não passa por `contracts/jobs.py`, que descreve
só o que atravessa máquina, e segue em `TIPOS_LOCAIS` — o claim do agente não a
alcança. O `import` só entraria em `POR_TIPO` se algum dia a origem fosse
exclusivamente remota.

Três coisas ficaram registradas por serem escolha, não consequência:

- **O executor não relê o payload por contrato, mas por um leitor só.**
  `services/imports.py: construir_importador` é chamada pelo POST (para validar
  a entrada e descrever a origem) e pelo executor (para reconstruir o
  importador). Ter um leitor é o que impede a chave renomeada de passar no POST
  e falhar no worker — o mesmo motivo do `contracts/`, sem precisar dele: os
  dois leitores são deste repositório e sobem no mesmo deploy.
- **A cerca de caminho é conferida nas duas leituras.** `import_root` é do
  processo que executa, e o worker pode ter outro.
- **Reclamar não é retentar.** O claim só pega `queued`; importação que travou
  no meio continua sendo fechada como falha, e refazê-la segue sendo decisão de
  quem lê a tela. Mudar isso seria mudar política de retentativa junto, e não é
  o que este item pedia.

O risco que a mudança criou tem guarda própria: desligar o laço embutido e
esquecer de subir o worker deixaria a importação parada na fila para sempre. O
recolhimento ganhou esse segundo caso, com mensagem diferente (reimportar não
conserta fila sem executor) e com a guarda `NOT EXISTS (importação running)` —
fila que anda não está abandonada, senão o terceiro de três dumps grandes seria
recolhido por esperar a vez.

### 6. Quebrar o `app.js` — `feito` (2026-08-21)
2.771 linhas, sem módulos, estado global mutável e zero teste. Era o item de
maior risco absoluto e o único adiado de propósito, porque mover JS sem teste
pede a aplicação no ar para conferir a olho.

Virou 16 módulos ES nativos, sem build (`<script type="module">`), o maior com
497 linhas: `nucleo` (o `$`, o `estado`, a porta HTTP, os escapes), `entrada`,
`dialogos`, `abas`, `datasets`, `roi`, `bucket`, `rotulagem`, `anotador`,
`treino`, `pool`, `projeto`, `modelos`, `prelabel`, `usuarios`, e `app` como
entrada. O corpo das funções não foi tocado: o corte foi por fatia de linha, e
os `import`/`export` saíram de uma varredura de identificadores sobre o código
sem comentário e sem literal de texto.

**`nucleo.js` não importa de ninguém, de propósito.** Todo módulo liga os seus
listeners no topo, chamando `$(...)`; se a raiz do grafo dependesse de alguém,
haveria uma ordem de avaliação em que o `$` ainda está na zona morta. Foi por
isso que `mostrarEntrada` — a resposta do `api()` ao 401 — mudou de módulo.

Duas coisas que a linguagem obrigou a mudar, ambas no `CLAUDE.md`:

- **Não se atribui à variável importada de outro módulo.** O fechamento
  genérico de diálogo fazia `pjClasses = []`; virou `esquecerClasses()`.
- **O parâmetro do `post` chamava-se `dados`,** o mesmo nome do estado da
  grade. Sem escopo de módulo ninguém notava; com ele, viraria import.

**A conferência foi com a aplicação no ar**, sobre uma instalação semeada com
14 imagens, um dataset selado, um treino de 12 épocas, um build de engine, uma
implantação e um pré-tag na fila. As sete abas foram capturadas antes e depois
e o texto renderizado bate; o anotador foi exercitado por evento sintético
(desenhar, trocar classe pelo teclado, mover, dar zoom, salvar, avançar) e
gravou de verdade. Os únicos diffs contra o baseline são a caixa que o próprio
teste gravou.

Dois defeitos vieram do fato de a aplicação ter subido — nenhum deles do
refactor:

- **`GET /v1/train-devices` respondia 422 para todo mundo.** A rota não tem
  `{project_slug}`, e `require_role` declara esse caminho: o FastAPI o pedia
  como parâmetro ausente. O diálogo de novo treino ficava sem máquina para
  fixar, e o `.catch(() => {})` do front engolia. Passou a exigir só
  autenticação, como `/deploy-devices` e pelo mesmo motivo — o que protege o
  dado é o `SELECT`, que devolve nome e estado.
- **A casca ia sem `Cache-Control`.** Uma `index.html` velha em cache pedindo o
  `app.js` novo como script comum derruba a página inteira com "Cannot use
  import statement outside a module". Aconteceu na própria migração, no
  navegador de conferência. Agora vai `no-cache`, e o etag faz disso um 304.

Ficou de fora: os módulos não têm teste. A garantia continua sendo a leitura e
a conferência a olho — o que mudou é que agora ela cabe num arquivo por vez.

### 7. Observabilidade — `feito` (2026-08-20)
Era: sem handler global de exceção, sem log configurado, sem request-id. Um 500
não se correlacionava com o request que o causou.

`api/logs.py` resolve os três com um mecanismo só (`b904904`). Cada requisição
ganha um código numa `ContextVar`; ele entra em toda linha de log emitida
durante ela **por um filtro no root**, e não por quem chama o logger — senão
cada `logger.info` do repositório teria de lembrar de passá-lo, e o
`%(request_id)s` do formato estouraria em log emitido fora de requisição
(`db.migrate`, script solto). O código volta no `X-Request-Id` e, no 500, no
corpo; o front já o mostra sem mudança nenhuma, porque lê `detail`.

Três decisões que ficaram no código:

- **O código do proxy é preservado, mas validado.** É o que correlaciona os
  dois lados; aceitar texto livre deixaria qualquer cliente escrever uma linha
  inteira de log, inclusive uma que parecesse legítima.
- **`/healthz` e o estático não geram linha de acesso.** O orquestrador bate no
  healthz a cada poucos segundos e afogaria o resto.
- **Formato é texto com o código no meio, não JSON.** Não há coletor nesta
  instalação; quando houver, o que muda é o formatter, e só ele.

Ficou de fora: o log de acesso do uvicorn continua saindo em paralelo, sem o
código. Desligá-lo é opção do comando de subida (`--no-access-log`), não do
código, e a duplicação incomoda menos que a decisão errada no lugar errado.

### 8. Menores — `feito` (2026-08-20)
Os três, em `93d8a9c`.

- **Liveness e readiness separadas.** `/healthz` não consulta mais nada — é a
  sonda que faz o orquestrador REINICIAR, e apontá-la para o banco fazia de uma
  oscilação do Postgres um interruptor do processo. `/readyz` responde a
  pergunta de tráfego, com 503 enquanto o banco não volta. O healthcheck do
  compose passou para `/readyz`.
- **Varredura de idioma: uma divergência em 70 rotas.** O segmento `origens` de
  `datasets/{slug}/origens/{source_id}` virou `sources` (os handlers já eram
  `add_origin`/`remove_origin`). Colunas e tabelas estão todas em inglês. O que
  NÃO foi alinhado, e por quê: chave de resposta e parâmetro de query seguem o
  que o `app.js` consome — a regra já dizia isso para JSON, e agora diz também
  para query; e os dois arquivos de migration com nome em português ficam,
  porque o nome é a chave em `schema_migrations` e renomear faria o runner
  reaplicá-las.
- **`dataset.selection` derrubada** pela 013. Estava morta desde a 007, e o
  comentário da 002 — que não se edita — segue descrevendo o modelo antigo em
  detalhe. Sem coluna não há o que interpretar errado.

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

### 12. Achados do review do item 11 — `feito` (2026-08-21)
O que o review de 2026-08-11 confirmou e ficou de fora da leva de correções.
O gatilho era "quando o arquivo for mexido a sério": foi agora — `samples.py`
virou router + `services/samples.py`, e as quatro limpezas foram junto.

- **A contagem e a página liam snapshots diferentes.** O item previa dois
  caminhos, e nenhum dos dois foi o escolhido: em vez de REPEATABLE READ (cara
  para uma contagem de tela) ou de a tela assumir a folga, o total passou a sair
  da **mesma consulta** da grade, por `count(*) OVER ()`. Um comando, um
  snapshot. Custa ordenar o conjunto filtrado inteiro em vez de parar nos 60 da
  página — trabalho que a contagem à parte já fazia de qualquer forma.
- **O `count` juntava `annotation` mesmo com `filtro=todas`.** A junção agora
  só entra se o filtro falar de `a`. Sobrou um caso: a página vazia, que não
  tem janela de onde tirar o total e cai numa contagem à parte — é lá que a
  condição vive.
- **O esqueleto `FROM ... WHERE` estava escrito duas vezes.** Virou
  `_de_onde(juncao, escopo, cond)`: a cláusula é escrita uma vez e as duas
  consultas a interpolam. A divergência que produziu o bug original ficou
  impossível em vez de remendada.
- **`test_prelabel_id_malformado_nao_e_500` rodava `_produzir_modelo` à toa.**
  O setup saiu, com o porquê na docstring: o 422 sai do pydantic antes da
  primeira linha do handler, e o `admin` é superusuário.

### 13. Contrato do payload de job — `feito` (2026-08-21)
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

**O terceiro leitor foi fechado em 2026-08-21.** `services/agents.py` lia o
payload cru em seis lugares (`alvos_prelabel`, `gravar_prelabels`,
`diretorio_manifesto`, `registrar_modelo`, `registrar_build`,
`registrar_avaliacao`); todos passaram por `job_com_payload`, e o default
`"detector"` do `model_slug`, que estava duplicado com o do produtor, saiu.

A decisão que faltava era o que fazer com os 404 de payload incompleto. A
resposta foi **conferir o tipo do job antes de ler**: o que barrava um agente
chamando `/build` num job de treino era o payload de treino não ter
`model_version_id` — um 404 que saía por acidente do `.get()`. Com o tipo
conferido, o 404 continua, e agora diz o que de fato aconteceu; um
`PayloadInvalido` no tipo certo é bug de quem enfileirou e vira 500 na rede de
`logs.correlacionar`, que é o tratamento correto para erro de programação.

O caminho mais barulhento não era esse: **`/targets` num job de treino
respondia 200 com lista vazia.** O escopo era lido com `.get()`, `None` virava
uma consulta que não casa com nada, e o agente recebia "nenhuma imagem para
prever" em vez de "este job não é seu". O teste novo varre as quatro rotas
alheias a um job de treino.

Ficou de fora, ainda:

- **`params` do treino continua `dict`.** Os defaults de `epochs`/`imgsz`/
  `batch` seguem duplicados entre o `RunIn` da rota e o executor. Não há
  `KeyError` a fechar ali (o executor já lê com `.get(chave, default)`), que é
  o risco que este item existia para tirar; virar dataclass aninhada é v2.
- **O job `import`.** Roda dentro do processo da API e não passa pelo agente;
  entra em `POR_TIPO` quando o item 5 o tornar reclamável.

### 14. Contrato de resposta — `feito` (2026-08-21)
Três rotas usavam `response_model` (as três de `routers/auth.py`); as demais
devolviam a linha do banco montada à mão. A linha **era** o DTO: coluna
acrescentada a um `SELECT` viajava para o navegador sem ninguém decidir, e o
OpenAPI não descrevia saída nenhuma.

Agora toda rota de usuário declara o que devolve, e
`tests/test_contrato_resposta.py` varre o OpenAPI atrás de quem não declarou —
rota nova sem contrato reprova ali, e não meses depois.

**A porta do agente fica de fora, de propósito.** O que `/v1/agent/*` devolve é
o payload do job, cuja descrição única é `contracts/jobs.py`; uma segunda em
pydantic seria a duplicação que aquele módulo existe para evitar — e
`contracts/` não pode importar pydantic, porque o dispositivo de borda o carrega
sem fastapi junto.

Os modelos foram escritos coluna a coluna a partir do `SELECT` de cada rota,
para que nada sumisse do que o `app.js` já lê (não há build nem lint no front:
chave que some não é detectada). Três chaves saíram de propósito, todas
internas e nenhuma lida pelo front: `project_id` e `proj` no detalhe do
dataset, `project_id` no detalhe do modelo — a linha as carregava só para o
handler resolver o RBAC.

Dois efeitos colaterais:

- **Cinco rotas tomavam id malformado como 500.** `GET .../jobs/{job_id}`,
  as três de `runs/{run_id}`, as de `users/{user_id}`, `me/tokens/{token_id}` e
  `me/sessions/{session_id}` recebiam `str` e deixavam o cast do Postgres
  estourar. Tipar como `UUID` na rota devolve 422, como já faziam
  `samples/{sample_id}` e o corpo do prelabel. O do job de projeto entrou com
  teste antes.
- **`target` do build não é texto, é `jsonb`.** Só apareceu porque o
  `response_model` reprovou: é a variante compilada
  (`{"imgsz": 640, "precision": "fp16"}`), e era ela que distinguia dois builds
  do mesmo formato sem que nada dissesse isso.

Fica em aberto: o `CLAUDE.md` dizia que não havia camada onde renomear chave de
JSON. Agora há — o modelo de saída. Renomear segue custando mexer no `app.js`,
mas o lugar existe.

### 15. Conexão como parâmetro do serviço — `em curso`
O `CLAUDE.md` explica que cada `fetch_*` pega uma conexão própria e que
sequência atômica pede `db.connection()` + `conn.transaction()`. Era convenção
conferida por quem revisa: nada no tipo impedia um serviço de fazer
leia-decida-escreva com três conexões diferentes.

**A forma ficou definida com `datasets`** (2026-08-24), a primeira migração a
carregá-la: toda função do módulo que fala com o banco recebe `conn: db.Conn`,
e quem **abre** a conexão — e, quando escreve, a transação — é a função pública
do serviço.

A decisão que o item não previa foi *quem* segura a conexão. A leitura literal
("o serviço recebe `conn`") põe o `async with db.connection()` no router, e aí
é o router que decide onde a transação começa e termina — regra de domínio, que
passaria a ser descrita em dois lugares. Segurar na função pública do serviço
mantém o router em uma linha e dá a mesma garantia onde ela importa: nenhum
auxiliar do congelamento consegue alcançar o pool por conta própria.

Como o `db.fetch_*` precisa seguir com `conn=None` opcional para os serviços
ainda não migrados, o que confere não é o tipo: é
`tests/test_datasets.py: test_servico_de_dataset_nunca_alcanca_o_pool_sozinho`,
que varre o módulo por AST e reprova chamada sem `conn=` (conferido quebrando
de propósito). Vale por módulo, conforme cada um for convertido.

**Segurar a conexão trouxe dois efeitos que a forma tem de prever**, os dois
achados no review da primeira migração e os dois de psycopg, não de desenho:

- `conn.transaction()` só abre transação de verdade se a conexão estiver
  ociosa. Depois de qualquer leitura ela está `INTRANS` e o bloco vira um
  `SAVEPOINT`, com o commit acontecendo quando o pool devolve a conexão. Como o
  serviço migrado lê antes de escrever quase sempre, a forma passou a ser abrir
  as duas no mesmo `async with` — e há varredura por AST para isso também.
- conexão aberta é transação aberta, então trabalho pesado que não fala com o
  banco não pode ter uma na mão. O `congelar` virou três fases (ler, montar,
  gravar) com a do meio sem conexão nenhuma; o teste afirma isso olhando o
  `pg_stat_activity` de fora, no momento em que o manifesto é montado.

`projects` veio em seguida (2026-08-24) e a forma se sustentou sem ajuste — o
único caso novo foi o papel do usuário, que entra na resposta do detalhe:
`deps.project_role` ganhou `conn=` opcional, como os helpers de `db.py`, e a
varredura passou a cobri-la.

Faltam os serviços de `agents`, `imports`, `models`, `samples` e `training`, e
os routers que ainda não têm serviço nenhum (`auth`, `buckets`, `devices`,
`jobs`, `prelabel`, `users`).

### 16. Porta de storage — `feito` (2026-08-20)
O acesso ao GCS está espalhado e sem porta. `routers/samples.py:63` faz
`from .buckets import _client` — um router entrando no helper privado de outro,
o único import cruzado entre routers da árvore. Quatro lugares constroem
`storage.Client()` (`samples.py:61`, `buckets.py:37,54`,
`importers/gcs_prefix.py:41,57`), e o de `samples` constrói um por requisição,
dentro do threadpool, a cada carga de página da galeria.
`importers/base.Importer` é a única porta de verdade que existe hoje.

`api/storage.py` (`cliente`, `pode_assinar`, `assinar`) colapsou os quatro e
matou o import cruzado. Ficou de fora `listar`/`baixar`: a navegação de bucket
assina pelo próprio blob que a listagem já devolveu, e passar por `assinar()`
custaria uma segunda ida ao GCS por miniatura — abstrair aquilo pioraria o que
o módulo existe para fazer.

O ganho que não estava previsto foi o teste: com o cliente atrás de uma função,
o dublê entra por `monkeypatch` e dá para afirmar o que a tela vê quando dá
errado — sem credencial, com credencial que não assina, com URI que não é
`gs://`, e com um objeto inalcançável no meio da página, que antes poderia
levar junto todas as miniaturas. Eram cinco caminhos sem nenhuma afirmação.

O cliente também passou a ser construído uma vez: a galeria construía um por
carga de página, dentro do threadpool.

### 17. `/train-devices` sem cerca de projeto — `pendente`
Achado do review de 2026-08-24, e é decisão, não conserto. O `cab0590`
corrigiu o 422 tirando o `require_role("engineer")` — papel é por projeto e a
rota não tem projeto no caminho —, e o que ficou no lugar é `CurrentUser`: um
`viewer`, ou um usuário que não é membro de projeto nenhum, lista o `slug`, o
`name` e o `status` de toda máquina de treino da instalação. `/deploy-devices`
já era assim, então isto é consistência, não novidade; mas é a instalação
inteira visível a quem tem conta, e vale escolher de propósito.

Duas saídas: assumir que nome e estado de máquina não são segredo dentro da
instalação (e escrever isso), ou exigir participação em ao menos um projeto —
que é um conceito de RBAC que hoje não existe em lugar nenhum, e teria de
valer para as duas rotas.

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
- **2026-08-20** — metade barata do item 5 feita (`dfc39bb`). Importação passou
  a bater ponto e ganhou reaper próprio em `services/imports.py`, chamado nas
  duas telas de jobs; o desfecho ficou guardado por `AND status = 'running'`,
  pela mesma razão que o `finish` do agente. Ficou registrado no item o que a
  investigação mostrou e o plano supunha ao contrário: a metade cara não é
  "importação vira job do agente" — o agente de borda não alcança o
  `import_root`, que está no disco da API —, e sim um segundo processo do mesmo
  código reclamando da mesma fila. Suíte: **181 testes passando** (eram 177).
- **2026-08-20** — item 16 feito (`0ee0a99`). `api/storage.py` com o cliente
  memoizado e a assinatura de URL; `buckets`, `samples` e o importador de
  prefixo passaram a usá-lo, e o único import cruzado entre routers
  (`samples.py` puxando o `_client` privado de `buckets.py`) morreu junto. Os
  cinco testes novos são de comportamento que nunca teve rede: o que a galeria
  mostra quando a credencial não assina, quando não há credencial, e quando um
  objeto do meio da página não pode ser assinado. Suíte: **186 testes
  passando** (eram 181).
- **2026-08-20** — item 7 feito (`b904904`). Código de correlação por
  requisição, filtro no root para que ele entre em toda linha de log sem quem
  chama precisar saber, rede para o erro não previsto (500 com o código no
  corpo, traceback no log sob o mesmo código) e linha de acesso por requisição.
  Os seis testes novos fixam o que dá para diagnosticar depois: o código volta
  no cabeçalho, o do proxy atravessa, o forjado com quebra de linha não, o erro
  não previsto não vaza detalhe interno, o erro de usuário continua sendo o
  status que era, e a linha de log do 500 carrega o mesmo código que o usuário
  vê. Suíte: **192 testes passando** (eram 186).
- **2026-08-20** — item 8 feito (`93d8a9c`). `/healthz` parou de consultar o
  banco e ganhou o par `/readyz`; o segmento de rota em português virou inglês
  (era a única divergência da convenção em 70 rotas); a 013 derrubou
  `dataset.selection`. Os três testes novos fixam o que a separação das sondas
  significa: viva sem banco, pronta só com ele, e 503 — não 500 — quando ele
  não responde. Suíte: **195 testes passando** (eram 192).
- **2026-08-20** — review dos 11 commits da sessão (alto esforço sobre
  `f0897ff..HEAD`). Cinco achados, os cinco procedentes, os cinco corrigidos
  com teste antes. Um é grave e nasceu do próprio item 5: ao ganhar
  `heartbeat_at`, a importação virou reclamável por qualquer máquina — o claim
  casa `requires` vazio com todo device e não filtra por tipo —, e o agente a
  encerraria como "tipo não suportado" por cima de quem a executa, ganhando
  sempre por ter prazo menor (`a89582a`, com a invariante escrita no
  `CLAUDE.md`). Junto foi o evento do recolhimento, que podia colidir no
  `max(seq)+1` com a task viva e abortar o recolhimento inteiro, devolvendo 500
  na tela de jobs.
  O segundo dói mais: o executor de prelabel tinha **voltado** a ler o payload
  cru (`a0e0087`). Um `git checkout` de conferência — usado para desfazer a
  mutação com que testei o `mypy` — reverteu o arquivo para antes do commit do
  contrato, e nada acusou, porque `job["payload"]` é `Any` e nenhum teste roda
  os executores. O item 13 entregou três dos quatro que prometeu. A guarda nova
  varre os quatro executores por `POR_TIPO`, que até então não tinha leitor.
  Os outros dois eram arestas do item 7 (`49e4e5e`): `$` casa antes de `\n`
  final, então o código forjado com quebra no fim passava pela validação de
  injeção de log; e `VISIONYARD_LOG_LEVEL=debug` derrubava a subida, porque o
  `logging` só aceita maiúsculas. Suíte: **200 testes passando** (eram 195).
- **2026-08-21** — itens 12 e 14 feitos. `samples.py` virou router +
  `services/samples.py` (o gatilho que o item 12 esperava), e as quatro
  limpezas foram junto: o total agora sai da mesma consulta da grade por
  `count(*) OVER ()` — um comando, um snapshot —, a junção com `annotation` só
  entra quando o filtro fala dela, o `FROM ... WHERE` é escrito uma vez, e o
  teste de id malformado do prelabel perdeu o setup que nenhuma asserção lia.
  O item 14 alcançou **todas** as rotas de usuário, com
  `tests/test_contrato_resposta.py` varrendo o OpenAPI para que rota nova sem
  contrato reprove. Dois defeitos vieram junto, achados pelo próprio modelo:
  cinco rotas tomavam id malformado como 500 (agora 422, como as demais), e o
  `target` do build era declarado texto quando é `jsonb` — a variante compilada
  que distingue dois builds do mesmo formato. Suíte: **204 testes passando**
  (eram 200).
- **2026-08-21** — item 6 feito. O `app.js` de 2.771 linhas virou 16 módulos ES
  nativos, sem build, o maior com 497 linhas; o corpo das funções não foi
  tocado. A conferência foi com a aplicação de fato no ar, sobre uma instalação
  semeada, comparando o texto das sete abas antes e depois e exercitando o
  anotador por evento sintético até o save. Dois defeitos apareceram por causa
  disso, nenhum do refactor: `/train-devices` respondia 422 para todo mundo
  (papel por projeto numa rota sem projeto no caminho), e a casca ia sem
  `Cache-Control`, o que na própria migração deixou uma `index.html` em cache
  pedindo o módulo como script comum. Suíte: **205 testes passando** (eram
  204).
- **2026-08-21** — item 13 fechado por inteiro: o terceiro leitor. Os seis
  lugares de `services/agents.py` que liam `job["payload"]` cru passaram a ler
  pelo contrato, através de `job_com_payload`, e o default `"detector"` do
  `model_slug` — duplicado com o do produtor — saiu junto. A decisão que faltava
  foi conferir o **tipo do job** antes de ler: o 404 que barrava um agente numa
  rota alheia saía por acidente da chave faltando, e agora sai de propósito. O
  achado do caminho foi `/targets` num job de treino, que respondia 200 com
  lista vazia — o escopo lido com `.get()` virava `None` e a consulta não casava
  com nada. Suíte: **206 testes passando** (eram 205).
- **2026-08-24** — review dos três commits de código de 21/08 (alto esforço,
  `8f576f9..HEAD`). Quatro achados, três corrigidos com teste antes. O grave é
  regressão do próprio item 13: `diretorio_manifesto` passou a exigir o
  contrato de treino, e `/manifest` virou 404 para todo job de avaliação — a
  rota que o `agent/evaluate.py` chama antes de medir. A leitura crua não tinha
  o problema porque `dataset_version_id` está nos dois payloads; nada acusou
  porque a suíte só chegava ao manifesto pelo caminho do treino, e o teste do
  item 13 conferia que rota alheia dá 404, não que a própria continua servindo
  (`410cc76`, com a exceção escrita na invariante do `CLAUDE.md`).
  O segundo veio do item 6: o `?v=` alcança só o ponto de entrada, e os 15
  módulos importados iam sem diretriz de cache nenhuma — o `StaticFiles` manda
  etag e last-modified, e o navegador então aplica frescor heurístico. Um
  deploy que mexesse em `nucleo.js` entregaria o `app.js` novo sobre o
  `nucleo.js` velho (`b5ad9c3`); os dois testes de cache são os primeiros —
  nem a casca tinha um. O terceiro é higiene do mesmo item: `prelabel.js` e
  `anotador.js` importavam `modelos` e o sombreavam com uma lista de mesmo nome
  (`70a23c3`). O quarto virou o item 17: `/train-devices` é decisão de acesso,
  não defeito. Suíte: **210 testes passando** (eram 206).
- **2026-08-24** — item 5 fechado. `api/worker.py` executa a importação
  reclamando da fila, dentro da API por default ou como `python -m api.worker`;
  o POST passou a enfileirar `queued` em vez de executar. `routers/imports.py`
  perdeu o `_run_job`, a cerca de caminho, a construção do importador e o
  upsert de origem — foram todos para `services/imports.py`, que era o gatilho
  que o `CLAUDE.md` previa para essa rota. A conferência que vale foi fora da
  suíte: API no ar com o laço desligado, POST respondendo `queued`, o job
  ainda `queued` um segundo depois, e o `python -m api.worker` como processo
  separado reclamando e concluindo (3 imagens, 2 anotações), com saída limpa no
  SIGTERM. Os quatro testes novos fixam o que a suíte consegue afirmar: que
  quem pede não executa, que dois executores não pegam o mesmo job, que fila
  sem executor é recolhida com a mensagem certa, e que fila que anda não é.
  Suíte: **214 testes passando** (eram 210).
- **2026-08-24** — review do `dde88be` (alto esforço). Seis achados, os seis
  procedentes, os seis corrigidos; quatro com teste antes.
  Dois são consequência do que a fila trouxe e eu não tinha visto. A guarda que
  protege a fila do recolhimento olhava só `NOT EXISTS (import running)`, e
  isso não é prova de vida do executor: o UPDATE logo acima, na mesma
  transação, acaba de tirar do `running` justamente a que passou do prazo — e
  um dump grande passa minutos numa imagem só com o executor vivo do outro
  lado. Havia ainda o intervalo entre concluir uma e reclamar a próxima. A
  prova virou dupla: estar executando **ou** ter terminado há menos de um
  prazo. E `construir_importador` lia o payload com `[]` sob a justificativa de
  que os dois leitores sobem no mesmo deploy — o que este mesmo commit tornou
  falso, porque agora é o payload que atravessa o deploy, não o código. Toda
  chave passou a ser lida com default, pela mesma regra do `contracts/`.
  Um é do front e não estava no diff: `bucket.js` esperava a importação
  terminar **antes** de vincular a origem ao dataset, e a espera do `esperarJob`
  estoura em 6 min. Com a execução serializada, esperar a vez de outra
  importação passou a contar no mesmo orçamento — e a espera que estoura
  deixava a origem sem vínculo nenhum, com o usuário vendo um dataset vazio. O
  vínculo passou a ser feito antes: ele nunca dependeu de a catalogação ter
  terminado.
  Os outros três: `job.error` publicava `import_root` para papel `viewer` na
  recusa do executor (o caminho absoluto do servidor na tela de todo mundo);
  o `parar.set()` do lifespan era teatro, porque o `cancel()` da linha seguinte
  chegava antes de a task voltar a rodar — agora há uma graça de 5s, que é o
  que o laço ocioso precisa; e faltava teste para o caso que a fila tornou
  alcançável, um device reclamando enquanto uma importação está `queued` (o
  `TIPOS_LOCAIS` já a barrava, mas nada afirmava isso). Suíte: **219 testes
  passando** (eram 214).
- **2026-08-24** — `datasets` virou router + serviço, e o item 15 ganhou forma.
  As 20 consultas e a regra inteira saíram de `routers/datasets.py` (524 → 275
  linhas), incluindo o `freeze` com o lock consultivo; o router ficou só com
  corpo, `response_model`, RBAC e status. Junto foi a primeira aplicação do
  item 15: toda função de `services/datasets.py` que fala com o banco recebe
  `conn`, e quem abre a conexão é a função pública do serviço — a decisão que o
  item não previa, e que evita o router passar a descrever a transação. O que
  confere é um teste por AST, porque `db.fetch_*` precisa seguir com `conn=`
  opcional para os serviços ainda não migrados.
  Nenhuma mudança de comportamento pretendida, e nenhuma feita: a única
  candidata era o `AND archived_at IS NULL` do lookup de projeto, que ficou de
  fora de propósito — quem barra escrita em projeto arquivado é o
  `require_role`, com 409 e antes do handler, e repetir a regra no serviço a
  descreveria duas vezes. Suíte: **220 testes passando** (eram 219).
- **2026-08-24** — review da migração de `datasets` (alto esforço). A parte
  mecânica saiu limpa: o revisor extraiu todo literal SQL de antes e de depois
  e comparou — todas as consultas atravessaram idênticas. O que mudou sem eu
  querer foi a vida da conexão, e são as duas coisas que agora estão escritas
  na forma do item 15: `conn.transaction()` depois de uma leitura é
  `SAVEPOINT`, não transação (conferido no psycopg 3.3.4 instalado: `outer =
  False`, conexão segue `INTRANS` depois do bloco); e o `congelar` passou a
  montar o manifesto — CPU pura, o trecho mais demorado — com transação aberta,
  o que um `idle_in_transaction_session_timeout` mata no meio. Os dois testes
  novos afirmam as duas coisas e falhavam antes: um varre o módulo por AST
  atrás de `transaction()` longe do `connection()`, o outro espia o
  `pg_stat_activity` de fora enquanto o manifesto é montado. A varredura do
  `conn=` também apertou — deixava passar `from ..db import fetch_one` e o
  módulo apelidado — sem passar a acusar `conn.execute`, que é justamente a
  forma certa. Suíte: **222 testes passando** (eram 220).
- **2026-08-24** — `projects` virou router + serviço, com a forma do item 15 já
  corrigida pelo review anterior. 346 → 218 linhas no router e nenhuma consulta
  inline; `services/projects.py` leva a criação (projeto + owner + schema v1 na
  mesma transação) e o append de classes sob lock consultivo. Um caso novo:
  o papel do usuário entra na resposta do detalhe, e `deps.project_role`
  alcançava o pool — ganhou `conn=` opcional, como os helpers de `db.py`.
  Duas melhorias vieram de graça, das que só aparecem quando a leitura e a
  escrita passam a caber na mesma transação: a checagem de slug duplicado do
  `criar` e o `projeto_ou_404` do `alterar`/`arquivar` deixaram de acontecer
  numa conexão à parte, e o `UPDATE ... RETURNING` do `alterar` virou
  `fetch_row` — antes era `fetch_one` e um `None` viajava para o
  `response_model`, virando 500 em vez de dizer o que houve.
  Os dois guardas por AST saíram de `tests/test_datasets.py` para
  `tests/test_conexao_do_servico.py`, parametrizados pela lista de serviços
  migrados (conferidos quebrando de propósito nos dois). Suíte: **224 testes
  passando** (eram 222).