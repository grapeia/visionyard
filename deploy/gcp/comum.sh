# Carregado pelos dois scripts: lê `config.env` e deriva os nomes.
# shellcheck shell=bash

set -euo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAIZ="$(cd "$AQUI/../.." && pwd)"

if [[ ! -f "$AQUI/config.env" ]]; then
  echo "falta $AQUI/config.env — copie de config.env.example e preencha" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$AQUI/config.env"

for v in PROJECT_ID REGION DOMINIO PREFIXO SQL_TIER SQL_VERSAO \
         MAX_INSTANCIAS POOL_POR_INSTANCIA MEMORIA CPU; do
  if [[ -z "${!v:-}" ]]; then
    echo "config.env: $v está vazio" >&2
    exit 1
  fi
done

# Nomes. Bucket é global no GCP inteiro, por isso leva o projeto no nome.
SERVICO="${PREFIXO}-api"
JOB_MIGRAR="${PREFIXO}-migrar"
JOB_SEED="${PREFIXO}-seed"
CONTA="${PREFIXO}-api"
CONTA_EMAIL="${CONTA}@${PROJECT_ID}.iam.gserviceaccount.com"
REPO="${PREFIXO}"
IMAGEM_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/api"
INSTANCIA_SQL="${PREFIXO}-pg"
CONEXAO_SQL="${PROJECT_ID}:${REGION}:${INSTANCIA_SQL}"
BANCO="visionyard"
USUARIO_BANCO="visionyard"
SEGREDO_DSN="${PREFIXO}-database-url"
BUCKET_FRONT="${PREFIXO}-front-${PROJECT_ID}"
BUCKET_DADOS="${PREFIXO}-dados-${PROJECT_ID}"
BUCKET_MODELOS="${PREFIXO}-modelos-${PROJECT_ID}"
IP_LB="${PREFIXO}-ip"
NEG="${PREFIXO}-api-neg"
BACKEND_API="${PREFIXO}-api-backend"
BACKEND_FRONT="${PREFIXO}-front-backend"
URL_MAP="${PREFIXO}-lb"
URL_MAP_HTTP="${PREFIXO}-lb-http"
CERTIFICADO="${PREFIXO}-cert"
PROXY_HTTPS="${PREFIXO}-https-proxy"
PROXY_HTTP="${PREFIXO}-http-proxy"
REGRA_HTTPS="${PREFIXO}-https"
REGRA_HTTP="${PREFIXO}-http"

# Onde o bucket de dados aparece dentro do container (Cloud Storage FUSE).
MONTAGEM="/mnt/dados"

g() { gcloud --project "$PROJECT_ID" --quiet "$@"; }

passo() { printf '\n==> %s\n' "$*"; }

# `existe <comando de describe...>`: verdadeiro se o recurso já existe. Os
# scripts são reexecutáveis — o que já foi criado é pulado, não recriado.
existe() { g "$@" >/dev/null 2>&1; }
