#!/usr/bin/env bash
# Infraestrutura do visionyard no GCP — uma vez por instalação.
#
#   cp deploy/gcp/config.env.example deploy/gcp/config.env   # e preencha
#   deploy/gcp/provisionar.sh
#
# Reexecutável: o que já existe é pulado. Não apaga nada, e não roda a senha
# do banco de novo se o segredo já existe. A versão da API sobe depois, com
# `publicar.sh`.
#
# O desenho, em uma linha: um balanceador HTTPS num domínio só, com o front
# saindo de um bucket e `/v1/*` indo para o Cloud Run. Mesma origem para os
# dois é o que mantém o cookie de sessão SameSite=lax funcionando sem CORS —
# ver deploy/gcp/README.md.
source "$(dirname "${BASH_SOURCE[0]}")/comum.sh"

passo "APIs do projeto"
g services enable run.googleapis.com sqladmin.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  compute.googleapis.com iamcredentials.googleapis.com \
  storage.googleapis.com

passo "Artifact Registry ($REPO)"
existe artifacts repositories describe "$REPO" --location "$REGION" ||
  g artifacts repositories create "$REPO" --location "$REGION" \
    --repository-format docker

passo "Conta de serviço da API ($CONTA_EMAIL)"
existe iam service-accounts describe "$CONTA_EMAIL" ||
  g iam service-accounts create "$CONTA" \
    --display-name "visionyard — Control API"
# No projeto da instância, que com SQL_CONEXAO pode ser outro que não este.
if [[ -n "$CONEXAO_SQL" ]]; then
  gcloud --quiet projects add-iam-policy-binding "${CONEXAO_SQL%%:*}" \
    --member "serviceAccount:$CONTA_EMAIL" --role roles/cloudsql.client \
    --condition None >/dev/null
fi
# Assinar URL sem chave: a credencial do Cloud Run assina pelo IAM
# (`signBlob`), e precisa desta permissão sobre si mesma. Sem ela a galeria
# fica sem miniatura e as máquinas não baixam nada — a aba Parâmetros, em
# "testar acesso", acusa.
g iam service-accounts add-iam-policy-binding "$CONTA_EMAIL" \
  --member "serviceAccount:$CONTA_EMAIL" \
  --role roles/iam.serviceAccountTokenCreator >/dev/null

passo "Buckets"
# $2: `--public-access-prevention` (nada sai público, nem por engano) ou
# `--no-public-access-prevention`. A flag é booleana — `=enforced` é recusado.
criar_bucket() {
  existe storage buckets describe "gs://$1" ||
    g storage buckets create "gs://$1" --location "$REGION" \
      --uniform-bucket-level-access "$2"
}
# Dados: manifestos de versão e dumps de importação, montados no container.
# Manifesto de versão congelada é registro — versionamento ligado para que
# apagar por engano não seja perder a reprodutibilidade.
criar_bucket "$BUCKET_DADOS" --public-access-prevention
g storage buckets update "gs://$BUCKET_DADOS" --versioning >/dev/null
criar_bucket "$BUCKET_MODELOS" --public-access-prevention
# Front: HTML, CSS e JS, sem segredo nenhum. O balanceador serve o bucket, e
# para isso os objetos precisam de leitura pública. Uma política da
# organização que proíba acesso público barra o passo seguinte — aí é com quem
# administra a organização, não com este script.
criar_bucket "$BUCKET_FRONT" --no-public-access-prevention
g storage buckets update "gs://$BUCKET_FRONT" \
  --web-main-page-suffix index.html >/dev/null
g storage buckets add-iam-policy-binding "gs://$BUCKET_FRONT" \
  --member allUsers --role roles/storage.objectViewer >/dev/null

for b in "$BUCKET_DADOS" "$BUCKET_MODELOS"; do
  g storage buckets add-iam-policy-binding "gs://$b" \
    --member "serviceAccount:$CONTA_EMAIL" \
    --role roles/storage.objectAdmin >/dev/null
done
# Imagens: só leitura. A API lista, lê e assina; quem grava nesse bucket é o
# processo de captura, não o visionyard.
for b in ${BUCKETS_IMAGENS:-}; do
  g storage buckets add-iam-policy-binding "gs://$b" \
    --member "serviceAccount:$CONTA_EMAIL" \
    --role roles/storage.objectViewer >/dev/null
done

if [[ "$SQL_CRIAR" == sim ]]; then
  passo "Cloud SQL ($INSTANCIA_SQL, $SQL_TIER)"
  # ENTERPRISE e não o default: os tiers db-custom-* não existem na
  # ENTERPRISE_PLUS, que é o default de instância nova em algumas versões.
  existe sql instances describe "$INSTANCIA_SQL" ||
    g sql instances create "$INSTANCIA_SQL" --database-version "$SQL_VERSAO" \
      --edition ENTERPRISE --tier "$SQL_TIER" --region "$REGION" \
      --availability-type zonal --storage-auto-increase \
      --backup-start-time 03:00
  existe sql databases describe "$BANCO" --instance "$INSTANCIA_SQL" ||
    g sql databases create "$BANCO" --instance "$INSTANCIA_SQL"
else
  passo "Banco existente (SQL_CRIAR=nao): nenhuma instância é criada"
fi

passo "Segredo do DSN ($SEGREDO_DSN)"
# Existir não basta: `secrets create --data-file` cria o segredo e a versão em
# duas chamadas, e uma falha entre elas deixa um segredo vazio que os jobs não
# conseguem ler. E "ter uma versão habilitada" também não: o serviço lê
# `:latest`, que é a mais recente — habilitada ou não. É ela que se confere;
# sem ela utilizável, a senha é gerada de novo.
if [[ "$(g secrets versions describe latest --secret "$SEGREDO_DSN" \
           --format 'value(state)' 2>/dev/null)" == ENABLED ]]; then
  echo "já existe — a senha do banco não é trocada"
elif [[ "$SQL_CRIAR" == nao ]]; then
  # O DSN traz a senha: lido sem eco e passado pelo stdin, nunca por argv nem
  # pelo config.env, que fica no disco de quem opera.
  read -rsp "DSN do banco existente (postgresql://...): " DSN; echo
  [[ "$DSN" == postgres*://* ]] || { echo "DSN precisa começar com postgresql://" >&2; exit 1; }
  existe secrets describe "$SEGREDO_DSN" ||
    g secrets create "$SEGREDO_DSN" --replication-policy automatic
  printf '%s' "$DSN" | g secrets versions add "$SEGREDO_DSN" --data-file -
  unset DSN
else
  # Hex: entra no DSN sem escape de URL.
  SENHA="$(openssl rand -hex 24)"
  # `--password` põe a senha no argv do gcloud, visível em `ps` durante o
  # segundo que o comando leva. Aceito de propósito: o provisionamento roda uma
  # vez, na máquina de quem o opera, e o `sql users create` não tem outro jeito
  # de receber a senha. Rodá-lo num servidor compartilhado ou num CI é o caso
  # que pede outro caminho (a API REST com o corpo no stdin).
  if existe sql users describe "$USUARIO_BANCO" --instance "$INSTANCIA_SQL"; then
    g sql users set-password "$USUARIO_BANCO" --instance "$INSTANCIA_SQL" \
      --password "$SENHA"
  else
    g sql users create "$USUARIO_BANCO" --instance "$INSTANCIA_SQL" \
      --password "$SENHA"
  fi
  existe secrets describe "$SEGREDO_DSN" ||
    g secrets create "$SEGREDO_DSN" --replication-policy automatic
  # Socket do conector do Cloud SQL, que o Cloud Run monta em /cloudsql: sem
  # IP autorizado, sem senha trafegando fora do conector.
  printf 'postgresql://%s:%s@/%s?host=/cloudsql/%s' \
    "$USUARIO_BANCO" "$SENHA" "$BANCO" "$CONEXAO_SQL" |
    g secrets versions add "$SEGREDO_DSN" --data-file -
  unset SENHA
fi
g secrets add-iam-policy-binding "$SEGREDO_DSN" \
  --member "serviceAccount:$CONTA_EMAIL" \
  --role roles/secretmanager.secretAccessor >/dev/null

passo "Balanceador HTTPS ($DOMINIO)"
existe compute addresses describe "$IP_LB" --global ||
  g compute addresses create "$IP_LB" --global --ip-version IPV4
IP="$(g compute addresses describe "$IP_LB" --global --format 'value(address)')"

# O backend da API nasce vazio. O NEG que o liga ao Cloud Run é criado e
# anexado pelo `publicar.sh`, depois do deploy: a documentação do serverless
# NEG diz que ele só aponta para serviço que já existe, e o serviço nasce no
# primeiro `publicar.sh`. Até lá o balanceador responde 503 em /v1.
existe compute backend-services describe "$BACKEND_API" --global ||
  g compute backend-services create "$BACKEND_API" --global \
    --load-balancing-scheme EXTERNAL_MANAGED
existe compute backend-buckets describe "$BACKEND_FRONT" ||
  g compute backend-buckets create "$BACKEND_FRONT" \
    --gcs-bucket-name "$BUCKET_FRONT"

# Tudo é front, menos a API e as sondas. `/docs` e `/openapi.json` ficam de
# fora de propósito: o mapa da API inteira não precisa estar na internet.
BB="projects/$PROJECT_ID/global/backendBuckets/$BACKEND_FRONT"
BS="projects/$PROJECT_ID/global/backendServices/$BACKEND_API"
MAPA="$(mktemp)"
trap 'rm -f "$MAPA"' EXIT
cat > "$MAPA" <<YAML
name: $URL_MAP
defaultService: $BB
hostRules:
- hosts: ['*']
  pathMatcher: rotas
pathMatchers:
- name: rotas
  defaultService: $BB
  pathRules:
  - paths: ['/v1', '/v1/*', '/healthz', '/readyz']
    service: $BS
YAML
g compute url-maps import "$URL_MAP" --global --source "$MAPA"

existe compute ssl-certificates describe "$CERTIFICADO" --global ||
  g compute ssl-certificates create "$CERTIFICADO" --global \
    --domains "$DOMINIO"
existe compute target-https-proxies describe "$PROXY_HTTPS" --global ||
  g compute target-https-proxies create "$PROXY_HTTPS" --global \
    --url-map "$URL_MAP" --ssl-certificates "$CERTIFICADO"
existe compute forwarding-rules describe "$REGRA_HTTPS" --global ||
  g compute forwarding-rules create "$REGRA_HTTPS" --global \
    --load-balancing-scheme EXTERNAL_MANAGED --address "$IP_LB" \
    --target-https-proxy "$PROXY_HTTPS" --ports 443

# Porta 80 só redireciona: cookie `secure` não viaja em http, e um login em
# http:// pareceria funcionar e perderia a sessão no passo seguinte.
cat > "$MAPA" <<YAML
name: $URL_MAP_HTTP
defaultUrlRedirect:
  httpsRedirect: true
  redirectResponseCode: MOVED_PERMANENTLY_DEFAULT
YAML
g compute url-maps import "$URL_MAP_HTTP" --global --source "$MAPA"
existe compute target-http-proxies describe "$PROXY_HTTP" --global ||
  g compute target-http-proxies create "$PROXY_HTTP" --global \
    --url-map "$URL_MAP_HTTP"
existe compute forwarding-rules describe "$REGRA_HTTP" --global ||
  g compute forwarding-rules create "$REGRA_HTTP" --global \
    --load-balancing-scheme EXTERNAL_MANAGED --address "$IP_LB" \
    --target-http-proxy "$PROXY_HTTP" --ports 80

cat <<FIM

Pronto. Falta, fora daqui:

  1. DNS: um registro A de $DOMINIO para $IP
     (o certificado só é emitido depois disso; leva de 15 min a algumas horas:
      gcloud compute ssl-certificates describe $CERTIFICADO --global)
  2. deploy/gcp/publicar.sh            — sobe a API, migra o banco e o front
  3. o primeiro superusuário           — ver deploy/gcp/README.md

Bucket de modelos criado: $BUCKET_MODELOS
(configure na aba Parâmetros, junto com os buckets de imagens)
FIM
