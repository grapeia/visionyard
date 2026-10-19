#!/usr/bin/env bash
# Publica uma versão: imagem da API, migração do banco, serviço e front.
#
#   deploy/gcp/publicar.sh
#
# Precisa do `provisionar.sh` já rodado. A ordem é a que protege o banco: a
# migração roda ANTES do serviço novo, e se ela falhar o script para com a
# versão anterior ainda no ar — schema divergente derruba o deploy, não a API.
source "$(dirname "${BASH_SOURCE[0]}")/comum.sh"

cd "$RAIZ"
if [[ -n "$(git status --porcelain)" ]]; then
  # A tag é o commit: imagem construída de árvore suja levaria o nome de um
  # commit que não é o que está dentro dela.
  echo "árvore com alteração não commitada — commite antes de publicar" >&2
  exit 1
fi
TAG="$(git rev-parse --short=12 HEAD)"
IMAGEM="$IMAGEM_BASE:$TAG"

IP="$(g compute addresses describe "$IP_LB" --global --format 'value(address)')"

passo "Imagem $IMAGEM"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet >/dev/null
# linux/amd64 explícito: construída num Mac com ARM, a imagem não sobe no
# Cloud Run e o erro só aparece na hora do deploy.
docker build --platform linux/amd64 -t "$IMAGEM" .
docker push "$IMAGEM"

AMBIENTE="$(mktemp)"
trap 'rm -f "$AMBIENTE"' EXIT
# IP de origem (api/auth.py: client_ip). O Cloud Run entrega a conexão a
# partir de 169.254.0.0/16, e o balanceador acrescenta ao X-Forwarded-For o
# IP do cliente e depois o dele próprio. Confiar nesses saltos é o que faz o
# teto de login contar por pessoa — confira depois do primeiro deploy, como
# diz o README: se estiver errado, todo login parece vir do mesmo IP.
# Buckets e bucket de modelos ficam FORA daqui de propósito: definidos no
# ambiente, travariam o campo na aba Parâmetros, que é onde se configuram.
cat > "$AMBIENTE" <<YAML
VISIONYARD_COOKIE_SECURE: "true"
VISIONYARD_LOG_FORMAT: json
VISIONYARD_DB_POOL_MAX: "$POOL_POR_INSTANCIA"
VISIONYARD_MANIFEST_ROOT: $MONTAGEM/manifests
VISIONYARD_IMPORT_ROOT: $MONTAGEM/imports
VISIONYARD_TRUSTED_PROXIES: '["169.254.0.0/16", "35.191.0.0/16", "130.211.0.0/22", "$IP/32"]'
YAML

passo "Migração do banco ($JOB_MIGRAR)"
g run jobs deploy "$JOB_MIGRAR" --region "$REGION" --image "$IMAGEM" \
  --service-account "$CONTA_EMAIL" \
  "${FLAGS_SQL[@]}" \
  --set-secrets "VISIONYARD_DATABASE_URL=${SEGREDO_DSN}:latest" \
  --env-vars-file "$AMBIENTE" \
  --command python --args=-m,db.migrate \
  --max-retries 0 --task-timeout 10m
g run jobs execute "$JOB_MIGRAR" --region "$REGION" --wait

passo "Serviço ($SERVICO)"
# - ingress interno e balanceador: tira o *.run.app da internet. Com ele
#   aberto, alguém falaria direto com o serviço, e o X-Forwarded-For que
#   mandasse passaria por um salto em que a API confia. "Interno" ainda deixa
#   entrar quem está na VPC do projeto — esta instalação não usa VPC, mas
#   quem acrescentar uma precisa refazer essa conta.
# - --allow-unauthenticated: quem autentica é a própria API (sessão, token de
#   API, token de device); o IAM do Cloud Run na frente barraria o navegador.
# - CPU sempre alocada e uma instância mínima: a fila de importação roda
#   dentro do processo (`import_worker_embedded`), e com CPU só durante
#   requisição ela pararia entre uma e outra.
# - concorrência 40: requisição da API é curta, e o que pesa é o argon2 do
#   login (64 MiB), que hoje roda dentro do event loop — um login de cada vez
#   por instância. Mover para o threadpool é pendência registrada no README.
# - timeout 900 s: o items.jsonl de uma versão grande passa de 100 MB. O
#   backend serverless do balanceador tem prazo fixo de 60 min e ignora o
#   `timeoutSec` de 30 s, então quem corta antes é o Cloud Run.
# - sem sonda de vida (`--liveness-probe ""` a remove; só omitir manteria uma
#   que alguém tivesse configurado antes): o hash de um dump grande na importação roda dentro do
#   event loop, e uma liveness que não fosse respondida nesse meio tempo
#   mataria a instância no meio da importação. Sobra a de subida, que só
#   espera o /healthz responder uma vez.
g run deploy "$SERVICO" --region "$REGION" --image "$IMAGEM" \
  --service-account "$CONTA_EMAIL" \
  --execution-environment gen2 \
  --ingress internal-and-cloud-load-balancing \
  --allow-unauthenticated \
  --no-cpu-throttling --cpu-boost \
  --min-instances 1 --max-instances "$MAX_INSTANCIAS" \
  --concurrency 40 --cpu "$CPU" --memory "$MEMORIA" --timeout 900 \
  "${FLAGS_SQL[@]:---clear-cloudsql-instances}" \
  --set-secrets "VISIONYARD_DATABASE_URL=${SEGREDO_DSN}:latest" \
  --env-vars-file "$AMBIENTE" \
  --clear-volumes \
  --add-volume "name=dados,type=cloud-storage,bucket=${BUCKET_DADOS},mount-options=uid=10001;gid=10001" \
  --clear-volume-mounts \
  --add-volume-mount "volume=dados,mount-path=${MONTAGEM}" \
  --startup-probe "httpGet.path=/healthz,periodSeconds=2,failureThreshold=30" \
  --liveness-probe ""
# Depois de um rollback por `update-traffic --to-revisions`, o tráfego fica
# preso na revisão escolhida, e deploys seguintes criam revisão nova sem
# tráfego nenhum: o script terminaria dizendo "publicado" com a API velha no
# ar. Devolver à mais recente é o que faz "publicar" querer dizer publicar.
g run services update-traffic "$SERVICO" --region "$REGION" --to-latest

passo "Balanceador → serviço ($NEG)"
# Aqui e não no provisionamento: o serverless NEG só aponta para serviço que
# já existe. A associação é conferida à parte da existência — criado o
# backend e falhado o `add-backend`, uma reexecução que só olhasse se o
# backend existe o deixaria vazio para sempre.
existe compute network-endpoint-groups describe "$NEG" --region "$REGION" ||
  g compute network-endpoint-groups create "$NEG" --region "$REGION" \
    --network-endpoint-type serverless --cloud-run-service "$SERVICO"
if ! g compute backend-services describe "$BACKEND_API" --global \
       --format 'value(backends[].group)' | grep -q "networkEndpointGroups/$NEG"; then
  g compute backend-services add-backend "$BACKEND_API" --global \
    --network-endpoint-group "$NEG" \
    --network-endpoint-group-region "$REGION"
fi

passo "Front (gs://$BUCKET_FRONT, versão $TAG)"
# Cada versão do front mora numa pasta própria, `static/<commit>/`, e a casca
# aponta para ela. Os módulos ES se importam por caminho relativo, então uma
# casca só carrega módulos da mesma versão — trocar arquivo por arquivo no
# mesmo prefixo deixava o navegador montar a página com um `nucleo.js` novo e
# um `app.js` velho no meio da publicação. A troca é a cópia da casca, um
# objeto só, e por isso o resto pode ir com cache longo: o nome muda a cada
# versão. A casca de cada versão fica como `index-<commit>.html`, que é o que
# o rollback do front copia de volta (ver README).
g storage rsync api/static "gs://$BUCKET_FRONT/static/$TAG" --recursive \
  --cache-control "public, max-age=31536000, immutable"
CASCA="$(mktemp)"
trap 'rm -f "$AMBIENTE" "$CASCA"' EXIT
sed -E "s#\"/static/([^\"?]+)(\?[^\"]*)?\"#\"/static/$TAG/\1\"#g" \
  api/static/index.html > "$CASCA"
if grep -qE '"/static/' "$CASCA" && grep -E '"/static/' "$CASCA" | grep -qv "/static/$TAG/"; then
  echo "a casca ainda referencia /static/ fora de static/$TAG — confira o sed" >&2
  exit 1
fi
g storage cp "$CASCA" "gs://$BUCKET_FRONT/index-$TAG.html" \
  --cache-control no-cache --content-type text/html
g storage cp "$CASCA" "gs://$BUCKET_FRONT/index.html" \
  --cache-control no-cache --content-type text/html

cat <<FIM

Publicado $TAG em https://$DOMINIO
  sonda:  curl -s https://$DOMINIO/readyz
  log:    gcloud logging read 'resource.labels.service_name="$SERVICO"' \\
            --project $PROJECT_ID --limit 50
FIM
