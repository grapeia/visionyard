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
        └─ dataset (regra de seleção viva)
             └─ dataset_version (snapshot imutável)
                  └─ job de treino → agente → training_run + métricas
                       └─ model_version → build (ONNX/TensorRT) → avaliação
                            └─ alias (challenger/production) → deploy
```

Duas invariantes sustentam o resto:

- **Dataset é regra, versão é snapshot.** O `dataset` guarda um filtro
  (`{"filter": {"metadata.linha": "A"}, "annotation_status": ["reviewed"]}`);
  o `dataset_version` congela a lista concreta de imagens e anotações. Só a
  versão é treinável, e ela nunca muda — é o que torna dois treinos
  comparáveis.
- **Anotação é por (imagem, autor).** Salvar mexe só na sua; o trabalho de
  outro rotulador não é tocado. É o que permite medir concordância depois.

## Componentes

| | |
|---|---|
| `api/` | Control API (FastAPI) + interface web servida estática, sem build |
| `agent/` | worker que roda nas máquinas de execução e reclama jobs |
| `db/` | migrations SQL versionadas, runner e seed do primeiro usuário |
| `tests/` | suíte da API contra Postgres real |

A interface tem as abas Projeto, Datasets, Rotulagem, Treinamento, Modelos,
Pool (máquinas e fila de tarefas) e Usuários.

## Subir

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

## Configuração

Tudo por variável de ambiente com prefixo `VISIONYARD_` (lidas de `.env`).
As que importam no primeiro dia:

| Variável | Para quê |
|---|---|
| `VISIONYARD_DATABASE_URL` | DSN do Postgres |
| `VISIONYARD_BUCKETS` | JSON de buckets GCS que a instalação enxerga — sem isso a UI não navega imagem nenhuma |
| `VISIONYARD_IMPORT_ROOT` | raiz permitida para importação de arquivo; é uma cerca, não uma conveniência |
| `VISIONYARD_COOKIE_SECURE` | `true` em produção, atrás de TLS |
| `VISIONYARD_TRUSTED_PROXIES` | CIDR dos proxies que terminam TLS; vazio = sem proxy |

O acesso ao GCS usa `GOOGLE_APPLICATION_CREDENTIALS`. As imagens são servidas
ao navegador por URL assinada — a API emite a URL e não trafega bytes.

## Agentes

Cada máquina de execução roda `python -m agent.main` com um token emitido na
aba Pool. O agente anuncia capacidades (`train`, `infer`, `prelabel`), faz
long-poll, reclama o que sabe fazer e devolve progresso por época enquanto
treina. Ver [`agent/README.md`](agent/README.md).

## Testes

Precisam de um Postgres acessível — a suíte cria e derruba `visionyard_test`
a cada sessão, aplicando todas as migrations do zero.

```bash
.venv/bin/python -m pytest
```

## Importação

Além de catalogar um prefixo de bucket GCS (sem baixar nada: a listagem já
traz `md5Hash` e `size`), há um importador de dump do Label Studio. Ambos são
adaptadores de uma interface comum em `api/importers/`.
