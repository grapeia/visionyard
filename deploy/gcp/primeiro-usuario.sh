#!/usr/bin/env bash
# Cria o primeiro superusuário da instalação no Cloud SQL.
#
#   deploy/gcp/primeiro-usuario.sh voce@empresa.com "Seu Nome"
#
# A senha é lida sem eco e entra no job pelo Secret Manager, não por argumento
# nem por log: o que um job imprime vai para o Cloud Logging, e o `db.seed` só
# imprime a senha quando ele mesmo a gera. O segredo é apagado no fim.
source "$(dirname "${BASH_SOURCE[0]}")/comum.sh"

EMAIL="${1:?uso: primeiro-usuario.sh EMAIL NOME}"
NOME="${2:?uso: primeiro-usuario.sh EMAIL NOME}"
# `|` é o separador da lista de argumentos do job (ver abaixo): um nome com
# vírgula, que é comum, não pode virar dois argumentos.
[[ "$EMAIL$NOME" != *"|"* ]] || { echo "e-mail e nome não podem ter |" >&2; exit 1; }
# Nome único por execução: com nome fixo, uma segunda execução que falhasse
# no "já existe" apagaria, no trap, o segredo que a primeira ainda usa.
# Job e segredo com o MESMO sufixo aleatório: só o segundo do relógio deixava
# duas execuções no mesmo segundo dividirem o job, e o trap de uma apagá-lo
# antes de a outra rodar.
SUFIXO="$(date +%s)-$(openssl rand -hex 4)"
SEGREDO_SENHA="${PREFIXO}-seed-senha-${SUFIXO}"
JOB_SEED="${JOB_SEED}-${SUFIXO}"

read -rsp "senha para $EMAIL (mín. 12 caracteres): " SENHA; echo
read -rsp "de novo: " CONFIRMA; echo
[[ "$SENHA" == "$CONFIRMA" ]] || { echo "as senhas não conferem" >&2; exit 1; }
unset CONFIRMA
# Antes de criar qualquer recurso: senha vazia criaria um segredo sem versão,
# e curta seria recusada pelo `db.seed` só depois do job inteiro subir.
if (( ${#SENHA} < 12 )); then
  echo "a senha precisa de ao menos 12 caracteres" >&2
  exit 1
fi

printf '%s' "$SENHA" |
  g secrets create "$SEGREDO_SENHA" --data-file - --replication-policy automatic
unset SENHA
# O trap só depois de criado: o que ele apaga é sempre o que esta execução
# criou. O job vai junto — ele referencia o segredo, e sem ele seria um job
# que ninguém consegue reexecutar.
apagar() {
  g run jobs delete "$JOB_SEED" --region "$REGION" >/dev/null 2>&1 || true
  g secrets delete "$SEGREDO_SENHA" >/dev/null 2>&1 || true
}
trap apagar EXIT
g secrets add-iam-policy-binding "$SEGREDO_SENHA" \
  --member "serviceAccount:$CONTA_EMAIL" \
  --role roles/secretmanager.secretAccessor >/dev/null

IMAGEM="$(g run services describe "$SERVICO" --region "$REGION" \
  --format 'value(spec.template.spec.containers[0].image)')"

g run jobs deploy "$JOB_SEED" --region "$REGION" --image "$IMAGEM" \
  --service-account "$CONTA_EMAIL" \
  --set-cloudsql-instances "$CONEXAO_SQL" \
  --set-secrets "VISIONYARD_DATABASE_URL=${SEGREDO_DSN}:latest,VISIONYARD_SEED_PASSWORD=${SEGREDO_SENHA}:latest" \
  --command python \
  --args="^|^-m|db.seed|--email|$EMAIL|--name|$NOME" \
  --max-retries 0
g run jobs execute "$JOB_SEED" --region "$REGION" --wait

echo "pronto — entre em https://$DOMINIO com $EMAIL"
