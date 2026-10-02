# visionyard

Plataforma de dados e modelos para visão computacional: catalogar imagens,
anotar, congelar datasets versionados, despachar treino para as suas máquinas
e promover o modelo que vencer.

O que a distingue de um notebook de treino é o **despacho**: a API não treina.
Ela mantém o estado (projetos, imagens, anotações, datasets, runs, modelos) e
enfileira jobs que agentes rodando nas suas máquinas de GPU reclamam, executam
e devolvem — com logs, métricas por época e o peso publicado num bucket.

## Modelo mental

```
imagens (GCS ou importação)
   └─ anotação por autor, com histórico
        └─ dataset (lista viva de origens)
             └─ dataset_version (snapshot imutável)
                  └─ job de treino → agente → training_run + métricas
                       └─ model_version → build (ONNX/TensorRT) → avaliação
                            └─ alias (challenger/production) → deploy
```

Duas invariantes sustentam o resto:

- **Dataset é definição, versão é snapshot.** O `dataset` aponta para origens
  (prefixos de bucket, importações) e muda quando você quiser; o
  `dataset_version` congela a lista concreta de imagens e anotações e sorteia o
  split uma única vez. Só a versão é treinável, e ela nunca muda — é o que torna
  dois treinos comparáveis.
- **Anotação é por (imagem, autor).** Salvar mexe só na sua; o trabalho de
  outro rotulador não é tocado. É o que permite medir concordância depois.

## Componentes

| | |
|---|---|
| `api/` | Control API (FastAPI) + interface web servida estática, sem build |
| `agent/` | worker que roda nas máquinas de execução e reclama jobs |
| `api/worker.py` | executor da importação: a mesma fila, dentro da API ou como processo próprio |
| `contracts/` | o que os dois combinam: o payload de cada tipo de job. Só stdlib |
| `db/` | migrations SQL versionadas, runner e seed do primeiro usuário |
| `tests/` | suíte da API contra Postgres real |

A interface tem as abas Projeto, Datasets, Rotulagem, Treinamento, Modelos,
Pool (máquinas e fila de tarefas) e Usuários.

## Subir (desenvolvimento)

Requisitos: Python ≥ 3.10 e PostgreSQL ≥ 14.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-api.txt

cp .env.example .env      # ajuste VISIONYARD_DATABASE_URL
createdb visionyard
.venv/bin/python -m db.migrate
.venv/bin/python -m db.seed --email voce@empresa.com --name "Seu Nome"

.venv/bin/uvicorn api.main:app --reload --port 8000
```

A senha do seed é gerada e impressa uma única vez. Não existe auto-cadastro:
o primeiro usuário sai daí, os demais por convite na aba Usuários.

## Subir (produção)

O `docker-compose.yml` é o artefato de deploy — desenvolvimento não passa por
ele. Sobe o Postgres da instalação, aplica as migrations e serve a API.

```bash
cp .env.docker.example .env.docker    # preencha POSTGRES_PASSWORD e a conta de serviço
docker compose --env-file .env.docker up -d --build
docker compose --env-file .env.docker run --rm seed \
    --email voce@empresa.com --name "Seu Nome"
```

O `--env-file` não é opcional: `env_file:` alimenta o processo dentro do
container, mas os `${...}` do compose são resolvidos antes disso, e para esses
o compose lê o `.env` — que aqui é o do desenvolvimento.

Quatro coisas que o arquivo decide por você:

- **As migrations são um passo bloqueante.** O serviço `migrate` roda a cada
  `up` e a API só sobe se ele terminar em zero. Schema divergente derruba o
  deploy em vez de deixar a API atender contra um banco errado; reaplicar é
  inócuo (`nada a aplicar`).
- **A API escuta só no loopback** (`127.0.0.1:8000`), e o Postgres não publica
  porta nenhuma. Quem termina TLS é um proxy no host.
- **Vida e prontidão são sondas diferentes.** `/healthz` responde que o
  processo está de pé e não consulta nada; `/readyz` responde que dá para
  servir, e é ele que o healthcheck do compose usa. Quem levar isto para um
  orquestrador que reinicia por liveness precisa manter essa separação: apontar
  a sonda de reinício para o banco faz de uma oscilação do Postgres um
  interruptor do processo.
- **O IP de origem muda atrás de container.** A API só aceita
  `X-Forwarded-For` quando o salto imediato está em
  `VISIONYARD_TRUSTED_PROXIES`, e esse salto passa a ser o gateway da rede do
  compose — daí a subnet fixa `172.28.0.0/16`. Se o valor estiver errado, todo
  login parece vir do mesmo IP e o teto de tentativas por janela vira coletivo
  em vez de por pessoa.

O agente de execução não está no compose: ele roda nas máquinas de GPU e de
borda, onde o torch vem do fornecedor da plataforma. Ver
[`agent/README.md`](agent/README.md).

## Configuração

Tudo por variável de ambiente com prefixo `VISIONYARD_` (lidas de `.env`).
Buckets, bucket e prefixo de modelos e validade da URL do agente também podem
ser definidos na aba **Parâmetros** (superusuário), sem reiniciar — mas a
variável, quando definida, vence e trava o campo na tela. A mesma aba testa o
que a credencial da API consegue fazer em cada bucket.
As que importam no primeiro dia:

| Variável | Para quê |
|---|---|
| `VISIONYARD_DATABASE_URL` | DSN do Postgres |
| `VISIONYARD_BUCKETS` | JSON de buckets GCS que a instalação enxerga — sem isso a UI não navega imagem nenhuma |
| `VISIONYARD_IMPORT_ROOT` | raiz permitida para importação de arquivo; é uma cerca, não uma conveniência |
| `VISIONYARD_IMPORT_WORKER_EMBEDDED` | `false` tira a execução da importação do processo da API — aí `python -m api.worker` precisa estar no ar |
| `VISIONYARD_COOKIE_SECURE` | `true` em produção, atrás de TLS |
| `VISIONYARD_TRUSTED_PROXIES` | CIDR dos proxies que terminam TLS; vazio = sem proxy |
| `VISIONYARD_MODELS_BUCKET` | bucket onde treino e export publicam (prefixo em `VISIONYARD_MODELS_PREFIX`, default `models`) — sem ele não há onde publicar |

O acesso ao GCS usa `GOOGLE_APPLICATION_CREDENTIALS`, e só a API o tem. As
imagens são servidas ao navegador por URL assinada, e ao agente também: a API
assina, por job, o que aquele job lê e onde ele grava, e não trafega bytes.
Por isso a credencial precisa **assinar** — chave de conta de serviço, ou, com
a credencial da VM/Cloud Run, a permissão `iam.serviceAccounts.signBlob` na
própria conta. Sem isso a galeria mostra só o lugar da imagem e o agente não
consegue baixar nada (503).

## Agentes

Cada máquina de execução roda `python -m agent.main` com um token emitido na
aba Pool. Faz long-poll, reclama o que sabe fazer e devolve progresso por época
enquanto treina. Quem decide o que a máquina sabe fazer (`train`, `infer`,
`prelabel`) é o admin, no cadastro do device: o agente só acrescenta
diagnóstico (GPU, CUDA, versão do torch), e a API descarta o resto — senão um
dispositivo de borda se declararia apto a treinar. Ver
[`agent/README.md`](agent/README.md).

## Testes e verificação

Os testes precisam de um Postgres acessível — a suíte cria e derruba
`visionyard_test` a cada sessão, aplicando todas as migrations do zero.

```bash
.venv/bin/pip install -r requirements-dev.txt   # ruff e mypy

.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/mypy
```

São exatamente os três comandos que o CI roda
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)), em push na `main` e
em pull request, contra um Postgres de serviço. A configuração dos três está
no `pyproject.toml`.

## Importação

Além de catalogar um prefixo de bucket GCS (sem baixar nada: a listagem já
traz `md5Hash` e `size`), há um importador de dump do Label Studio. Ambos são
adaptadores de uma interface comum em `api/importers/`.

A rota **enfileira**; quem executa é `api/worker.py`. Por default o laço dele
mora dentro do processo da API — uma instalação de uma máquina não precisa
subir nada a mais. Numa instalação onde a importação disputa recurso com quem
está usando a tela, ou com mais de uma réplica da API, tire o laço de lá:

```bash
VISIONYARD_IMPORT_WORKER_EMBEDDED=false   # no ambiente da API
.venv/bin/python -m api.worker            # e este processo no ar
```

Não é um agente, e não pode ser: o executor precisa enxergar
`VISIONYARD_IMPORT_ROOT`, que está no disco de quem serve a API — uma máquina
de GPU ou de borda não alcança esse caminho. Por isso a importação não passa
por `contracts/`, e por isso o claim do agente a ignora.

Subir o worker sem desligar o laço embutido não duplica trabalho (o claim é
`FOR UPDATE SKIP LOCKED`), mas **desligá-lo sem subir o worker** deixa a
importação parada na fila: depois de `VISIONYARD_IMPORT_JOB_STALE_SECONDS` ela
é fechada como falha, com uma mensagem dizendo que o processo não está no ar.
