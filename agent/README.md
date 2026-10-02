# Agente de execução visionyard

Worker que roda nas máquinas de execução (servidores de GPU para treino,
dispositivos de borda para inferência). Faz *long-poll* na Control API, reclama
jobs compatíveis com suas capacidades, executa e devolve logs, métricas e o
modelo treinado.

## Como funciona

```
register → claim → (baixa manifesto) → treina → heartbeat/logs/métricas → publica peso → finish
```

A máquina não tem credencial de nuvem. Imagens, peso e build vêm do bucket
por URL que a API assina para aquele job (`/jobs/{id}/urls`, em lotes), e o
peso treinado sobe por um PUT assinado para o caminho que a API escolhe
(`/jobs/{id}/upload`) — ela só aceita registrar o modelo que estiver lá.

O *claim* só entrega um job cujas exigências (`requires`) estão contidas nas
capacidades (`capabilities`) do device. Quem define as flags de roteamento
(`train`/`infer`) é o admin, ao cadastrar o device na aba **Pool** da interface.
O agente apenas acrescenta o diagnóstico (GPU, CUDA).

## Instalação

```bash
# 1. torch primeiro — a roda depende da plataforma:
#    servidor de GPU:     pip install torch --index-url https://download.pytorch.org/whl/cu121
#    dispositivo de borda: use o wheel do fornecedor para a sua plataforma
pip install -r agent/requirements.txt
```

O agente roda a partir da **raiz do repositório**, e não da pasta `agent/`
sozinha: além dela vai `contracts/`, onde mora o contrato do payload de job que
a Control API grava e os executores leem. É código sem dependência nenhuma
além da stdlib — não muda o que precisa ser instalado na máquina.

## Configuração (variáveis de ambiente)

| Variável | Obrigatória | Para quê |
|---|---|---|
| `VISIONYARD_API_URL` | sim | `http://api.interna:8000` |
| `VISIONYARD_AGENT_TOKEN` | sim | token do device (emitido na aba **Pool** → Máquinas → criar) |
| `VISIONYARD_WORK_DIR` | não | onde materializa dataset e roda o treino (default `./agent-work`) |
| `VISIONYARD_DEPLOY_DIR` | não | onde o build implantado fica para o coletor ler (default `./visionyard-deploy`) |
| `VISIONYARD_POLL_SECONDS` | não | intervalo entre polls quando ocioso (default 5) |
| `VISIONYARD_HEARTBEAT_SECONDS` | não | intervalo do ponto de presença quando ocioso (default 30) |

O bucket de modelos (`VISIONYARD_MODELS_BUCKET`) deixou de ser configuração do
agente: é da API, que escolhe onde publicar. **Atualize a API antes dos
agentes** — o agente 0.2 depende das rotas de assinatura, e a API nova recusa
build registrado no caminho antigo do agente 0.1 (`v<N>/builds/<arquivo>`,
que dois exports da mesma versão sobrescreviam).

Enquanto houver agente 0.1 no ar, não mude o bucket nem o prefixo de modelos
(aba **Parâmetros**): ele publica no caminho que calcula sozinho, e a API só
o aceita se ainda for o caminho atual — o treino termina em falha, com o peso
no bucket sem versão registrada. O agente 0.2 não tem esse problema: a API
confere contra o destino que ela própria entregou.

## Rodar

```bash
python -m agent.main
```

Sob PM2 (como o resto da borda):

```bash
pm2 start "python -m agent.main" --name visionyard-agent --interpreter bash \
  --cwd /caminho/do/repo
```

## Cadastrar o device antes

Na interface, aba **Pool** → **Máquinas** (superusuário):

- **Servidor de GPU**: `capabilities = {"train": true}`
- **Dispositivos de pré-tag**: `capabilities = {"infer": true, "prelabel": true}`
- **Dispositivo de produção**: cadastre e deixe em `draining` — não pega job
  enquanto serve a linha.

Copie o token exibido na criação para `VISIONYARD_AGENT_TOKEN` — ele não é mostrado
de novo.
