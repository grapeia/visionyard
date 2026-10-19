# Deploy no GCP: API no Cloud Run, front num bucket

```
                      https://DOMINIO  (um IP, um certificado)
                                │
                  Balanceador HTTPS externo (global)
                 ┌──────────────┴───────────────┐
      /v1/*, /healthz, /readyz            todo o resto
                 │                               │
        Cloud Run (API)                 bucket do front
        ├─ Cloud SQL (socket /cloudsql)  index.html, static/*
        ├─ bucket de dados montado em /mnt/dados (manifestos, importação)
        └─ GCS de imagens e de modelos — pela identidade do serviço
```

Três scripts, todos reexecutáveis:

| Script | Quando |
|---|---|
| `provisionar.sh` | uma vez por instalação: APIs, conta, buckets, Cloud SQL, segredo, balanceador |
| `publicar.sh` | a cada versão: imagem, migração, serviço, front |
| `primeiro-usuario.sh` | uma vez: o primeiro superusuário |

## Por que um balanceador, e não o bucket direto

O front chama a API em `/v1/...`, na mesma origem, e a sessão é um cookie
`SameSite=lax`. Servido de `storage.googleapis.com` e chamando `*.run.app`, o
front viraria outra origem: o cookie não iria nas chamadas, e fazê-lo ir
pediria `SameSite=None` + CORS com credenciais, ou seja, abrir mão da defesa
de CSRF que o `lax` dá de graça. O bucket sozinho também não serve HTTPS em
domínio próprio. Com o balanceador, front e API são um domínio só, e nada no
código muda.

## Passo a passo

Pré-requisitos: `gcloud` autenticado (`gcloud auth login`), faturamento
ligado e `docker`.

A conta que roda os scripts precisa de **Proprietário** no projeto. Editor não
basta: o provisionamento concede papéis (`setIamPolicy`) e o deploy anexa uma
conta de serviço ao Cloud Run (`iam.serviceAccounts.actAs`), e nenhuma das
duas vem no Editor. Sem Proprietário, o conjunto mínimo é: Project IAM Admin,
Service Account Admin, Service Account User, Cloud Run Admin, Cloud SQL Admin,
Secret Manager Admin, Storage Admin, Compute Load Balancer Admin, Compute
Public IP Admin, Artifact Registry Admin e Service Usage Admin.

```bash
cp deploy/gcp/config.env.example deploy/gcp/config.env    # preencha
deploy/gcp/provisionar.sh
```

**Banco que já existe.** Com `SQL_CRIAR=nao` o provisionamento não cria
instância, banco nem usuário: pede o DSN sem eco e o guarda no segredo. Banco
e usuário precisam existir; as tabelas vêm da migração do `publicar.sh`. Se o
banco é Cloud SQL, preencha `SQL_CONEXAO` (`projeto:regiao:instancia`) para o
socket `/cloudsql` ser montado. A conta da API recebe `cloudsql.client` no
projeto da instância, o que pede permissão de IAM lá também. Sem
`SQL_CONEXAO`, o DSN precisa alcançar o banco pela internet. Banco só em IP
privado de VPC pede Direct VPC egress, que estes scripts não configuram, e
muda a conta de proxies confiáveis descrita abaixo. Para trocar o DSN depois:
`printf '%s' "$DSN" | gcloud secrets versions add visionyard-database-url --data-file -`.

O provisionamento imprime o IP do balanceador. Crie o registro **A** do
domínio apontando para ele. O certificado gerenciado só sai depois disso — de
15 minutos a algumas horas:

```bash
gcloud compute ssl-certificates describe visionyard-cert --global \
  --format 'value(managed.status)'       # espere ACTIVE
```

Até o primeiro `publicar.sh`, o balanceador responde 503 em `/v1`: o
serverless NEG só pode apontar para um serviço que já existe, então é o
`publicar.sh` que o cria e o liga, depois de subir o serviço.

Depois, com tudo commitado (a tag da imagem é o commit):

```bash
deploy/gcp/publicar.sh
deploy/gcp/primeiro-usuario.sh voce@empresa.com "Seu Nome"
```

Entre em `https://DOMINIO` e, na aba **Parâmetros**:

1. **Buckets de imagens**: os que já existem. Cada um precisa de leitura para
   a conta `visionyard-api@…`. Os listados em `BUCKETS_IMAGENS` no
   `config.env` já receberam. Para os outros:
   `gcloud storage buckets add-iam-policy-binding gs://BUCKET --member serviceAccount:visionyard-api@PROJETO.iam.gserviceaccount.com --role roles/storage.objectViewer`
2. **Bucket de modelos**: o `visionyard-modelos-PROJETO` que o provisionamento
   criou.
3. **Testar acesso**: precisa dizer *assina URL*, e nenhum bucket pode
   aparecer com permissão faltando.

Os agentes passam a apontar para o domínio:
`VISIONYARD_API_URL=https://DOMINIO`. O agente fala com a mesma porta HTTPS
do navegador, e o balanceador manda `/v1/agent/*` para a API.

## Confira depois do primeiro deploy

**IP de origem.** O teto de login conta tentativas por IP, e a API só aceita o
`X-Forwarded-For` vindo de salto em que confia (`VISIONYARD_TRUSTED_PROXIES`,
montado no `publicar.sh` com a faixa interna do Cloud Run, as faixas do
balanceador do Google e o IP do próprio balanceador). Errou essa lista, e todo
login parece vir do mesmo endereço: o teto de 10 tentativas passa a valer para
todos os usuários juntos. **Esta lista não foi medida num deploy real.** Ela
segue a documentação do Cloud Run e do balanceador, mas precisa ser
conferida:

1. Erre a senha uma vez no login.
2. No Cloud SQL Studio (console), rode
   `SELECT ip, at FROM login_attempt ORDER BY at DESC LIMIT 3;`
3. O IP tem de ser o seu IP público (`curl -s ifconfig.me`). Se aparecer
   outro (`169.254.*`, o IP do balanceador, uma faixa do Google), veja o que
   chega à API. Ligue o debug, erre a senha de novo e leia a linha
   `client_ip:`, que mostra o salto que a API vê (`peer=`) e a cadeia inteira
   (`x-forwarded-for=`):

   ```bash
   gcloud run services update visionyard-api --region REGIAO \
     --update-env-vars VISIONYARD_LOG_LEVEL=debug
   gcloud logging read 'resource.labels.service_name="visionyard-api" jsonPayload.message:"client_ip:"' --limit 3
   gcloud run services update visionyard-api --region REGIAO \
     --remove-env-vars VISIONYARD_LOG_LEVEL
   ```

   O `peer` tem de estar na lista, assim como todo salto à direita do seu IP
   no `x-forwarded-for`. Ajuste a lista no `publicar.sh` até ser assim.

Um cuidado: o ingress "interno e balanceador" fecha o `*.run.app` para a
internet, mas ainda aceita quem estiver na VPC do projeto. Esta instalação não
usa VPC. Quem acrescentar uma precisa saber que, de dentro dela, alguém fala
com a API por um salto em que ela confia, e aí o `X-Forwarded-For` vale o que
ele quiser escrever.

**Sonda e log.**

```bash
curl -s https://DOMINIO/readyz          # {"status":"ok"}
gcloud logging read 'resource.labels.service_name="visionyard-api" severity>=WARNING' --limit 20
```

O log sai em JSON (`VISIONYARD_LOG_FORMAT=json`), com `severity` e
`request_id`. É pelo `request_id` que se procura o traceback de um 500 que um
usuário relatou: ele está no corpo da resposta.

## Decisões e o que elas custam

- **Uma instância sempre ligada, com CPU sempre alocada.** A fila de
  importação roda dentro do processo da API (`import_worker_embedded`). Com
  CPU só durante requisição, ela pararia entre uma requisição e outra. O custo
  é uma instância cobrada o tempo todo.
- **Instância que encolhe, ou que é trocada, derruba a importação em curso.**
  Isso acontece quando o Cloud Run reduz as instâncias, faz manutenção, e
  também **a cada `publicar.sh`**, porque a revisão antiga recebe SIGTERM e o
  laço tem 5 s de graça. A importação que estava nela para no meio, o
  `recolher_orfaos` a marca como interrompida, e reimportar resolve. Evite
  publicar com importação grande rodando. Para dumps
  grandes, o caminho é tirar o executor da API (`VISIONYARD_IMPORT_WORKER_EMBEDDED=false`)
  e rodar `python -m api.worker` à parte. Ele não serve HTTP, então seria um
  worker pool do Cloud Run ou uma VM pequena, montando o mesmo bucket de dados.
- **Disco é bucket.** Manifestos e dumps vão para `/mnt/dados`, que é o bucket
  de dados montado por Cloud Storage FUSE: o disco do container some a cada
  instância nova. Manifesto é gravado uma vez e lido depois, e é para esse uso
  que o FUSE serve bem. O versionamento do bucket está ligado, porque manifesto
  de versão congelada é registro.
- **Conexões do banco.** Cada instância abre até `POOL_POR_INSTANCIA`. O
  `--max-instances` limita por revisão, e durante uma publicação a revisão
  velha e a nova convivem. A conta segura é
  `2 × MAX_INSTANCIAS × POOL_POR_INSTANCIA`, mais o job de migração. Com os
  defaults isso dá 80 contra os ~100 do `db-custom-1-3840`.
- **Sem sonda de vida, só a de subida.** O hash de um arquivo grande na
  importação roda dentro do event loop. Uma liveness que ficasse sem resposta
  nesse intervalo mataria a instância no meio da importação.
- **Sem credencial em arquivo.** A API usa a identidade do serviço e assina
  URL pelo IAM (`signBlob`). O provisionamento concede essa permissão à conta
  sobre ela mesma. Nenhuma chave JSON existe nesta instalação.
- **`/docs` fora do balanceador.** O mapa da API inteira não precisa estar na
  internet. Para consultá-lo:
  `gcloud run services proxy visionyard-api --region REGIAO`.
- **As 2 ou 3 primeiras linhas de cada subida saem em texto.** O uvicorn as
  escreve antes de o app configurar o log, e o Cloud Logging as classifica
  como ERROR. São as de "Started server process", não falha.

## Pendências conhecidas

Não bloqueiam o deploy, mas ficam mais visíveis no Cloud Run do que numa
máquina só:

- **O argon2 do login roda dentro do event loop** (`api/auth.py`, nas chamadas
  a `verify_password`/`hash_password`). Cada login segura a instância inteira
  pelo tempo de uma verificação. Uma rajada de logins atrasa todas as outras
  requisições daquela instância. O conserto é mandar as duas chamadas para o
  threadpool.
- **O hash da importação de arquivo também** (`importers/runner.py`,
  `_hashes_and_size`). Por isso não há liveness.
- **O congelamento grava o manifesto com a transação aberta**
  (`services/datasets.py`). No Cloud Run essa gravação vai para o bucket, pelo
  FUSE. Uma versão grande deixa a conexão `idle in transaction` e o lock do
  dataset presos durante o upload.

## Atualizar e voltar atrás

`publicar.sh` de novo. A migração roda antes do serviço novo e, se falhar, a
versão anterior continua no ar.

Para voltar o código:

```bash
gcloud run revisions list --service visionyard-api --region REGIAO
gcloud run services update-traffic visionyard-api --region REGIAO \
  --to-revisions REVISAO_ANTERIOR=100
```

O `publicar.sh` seguinte devolve o tráfego à revisão nova
(`update-traffic --to-latest`). Sem isso, o rollback deixaria o tráfego preso
na revisão escolhida para sempre.

O front volta à parte. Cada versão fica em `static/<commit>/`, com a casca
correspondente em `index-<commit>.html`:

```bash
gcloud storage ls 'gs://visionyard-front-PROJETO/index-*'
gcloud storage cp gs://visionyard-front-PROJETO/index-COMMIT.html \
  gs://visionyard-front-PROJETO/index.html --cache-control no-cache
```

O banco não volta: migration só anda para a frente. Voltar o código depois de
uma migration só funciona se a versão anterior tolerar o schema novo. Coluna
nova com default tolera; coluna renomeada não.
