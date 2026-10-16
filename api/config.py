"""Configuração por ambiente. Nada de segredo com default utilizável."""
from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VISIONYARD_", env_file=".env",
                                      extra="ignore")

    database_url: str = "postgresql://postgres:postgres@localhost:5432/visionyard"

    # Cookie de sessão. secure=False só serve pra dev em http://localhost;
    # em produção o proxy termina TLS e isto vira True.
    cookie_name: str = "visionyard_session"
    cookie_secure: bool = False
    # Literal e não str: é o conjunto que o Set-Cookie aceita, e um valor fora
    # dele viraria cookie sem SameSite — sem CSRF nenhum barrado.
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    # Sessão expira em 12h de inatividade ou 7 dias absolutos — o teto absoluto
    # existe pra que uma sessão em uso contínuo não viva pra sempre.
    session_idle_hours: int = 12
    session_max_days: int = 7

    password_min_length: int = 8

    # Nível do log da aplicação. O acesso sai numa linha por requisição, com o
    # código de correlação; DEBUG só para investigar.
    log_level: str = "INFO"
    # `json` no Cloud Run: o Cloud Logging lê a severidade de cada linha só
    # nesse formato (ver `logs.FormatoJson`). `texto` para quem lê no terminal.
    log_format: Literal["texto", "json"] = "texto"

    # Conexões do pool POR PROCESSO. Num orquestrador que escala por instância
    # o total é isto vezes o número de instâncias, e o Cloud SQL recusa acima
    # do teto do tier — com 10 por instância, cinco instâncias já levam 50.
    db_pool_max: int = 10

    # Throttle de login. O teto duro é por IP — é o que barra força bruta sem
    # permitir que alguém tranque a conta de outro só errando a senha de
    # propósito. Por email há apenas backoff progressivo, com teto baixo:
    # atrasa o ataque e o usuário legítimo espera no máximo alguns segundos.
    login_max_attempts: int = 10          # por IP, na janela
    login_window_minutes: int = 15
    login_backoff_after: int = 3          # falhas antes de começar a atrasar
    login_backoff_max_seconds: int = 60

    # Validade do link de convite / redefinição de senha.
    invite_ttl_hours: int = 72

    # Buckets que a instalação enxerga. Digitar o nome à mão toda vez convida
    # a erro que só aparece como "nenhuma imagem encontrada"; e a lista também
    # limita o que a navegação pode ler.
    # Ex.: VISIONYARD_BUCKETS='["meu-bucket-imagens"]'
    buckets: list[str] = []

    # Raiz permitida para importação de arquivo (dumps do Label Studio e as
    # imagens deles). Sem essa cerca, o caminho vinha do cliente e a rota
    # respondia se um arquivo existe ou não em qualquer lugar do servidor — um
    # oráculo de filesystem — além de conseguir ler qualquer sqlite legível.
    import_root: str = "./data/imports"

    # Onde os manifestos de versão de dataset são gravados. Caminho local por
    # ora; quando houver conta de serviço na nuvem, passa a ser um gs://.
    manifest_root: str = "./data/manifests"

    # Importação que não bate ponto há este tempo é dada por interrompida. É
    # muito maior que o do agente porque o ponto da importação sai a cada lote
    # de leitura: um dump grande passa minutos numa imagem só (hash + dimensão
    # de arquivo enorme), e recolher cedo demais mataria job que está vivo.
    import_job_stale_seconds: int = 900

    # Onde a importação executa. `true` mantém o laço dentro do processo da
    # API, que é o comportamento histórico e o default: a instalação de uma
    # máquina só continua funcionando sem nada a mais para subir. `false` tira
    # a execução daqui — e aí quem reclama da fila é `python -m api.worker`,
    # que é o que impede o hash de um arquivo grande de disputar o event loop
    # com quem está atendendo requisição.
    import_worker_embedded: bool = True

    # De quanto em quanto o executor volta a olhar a fila. Enfileirar cutuca o
    # laço embutido, então este intervalo só governa o worker de fora e o
    # recolhimento — não a latência de começar uma importação.
    import_worker_poll_seconds: float = 2.0

    # Job cujo agente não bate ponto há este tempo é considerado órfão (reboot,
    # crash) e volta a ser reclamável por outro agente, respeitando max_attempts.
    # Precisa ser bem maior que o intervalo de heartbeat do agente, senão um
    # treino lento seria reclamado por engano e rodaria duas vezes.
    agent_job_stale_seconds: int = 180

    # Onde os pesos treinados e os builds compilados são publicados. Quem
    # escolhe o caminho é a API, não o agente: ela assina o PUT e depois só
    # aceita registrar um modelo que esteja debaixo do caminho que ela mesma
    # deu. Vazio significa que treino e export não têm onde publicar.
    models_bucket: str = ""
    models_prefix: str = "models"

    # Validade da URL assinada que o agente recebe para ler ou gravar no
    # bucket. Ele pede na hora de usar (lote de imagens, peso, upload no fim
    # do treino), então o prazo só precisa cobrir a transferência — não o job.
    agent_url_minutes: int = 60

    # De onde o SAM do anotador lê os pixels da imagem. "api": a API baixa do
    # bucket e entrega reduzida, do mesmo endereço da tela — funciona sem
    # configurar nada no bucket. "bucket": o navegador lê direto pela URL
    # assinada, poupando a API, mas aí o bucket precisa liberar CORS (GET)
    # para a origem da tela. Mostrar a imagem não depende disto: `<img>` não
    # passa pelo CORS; ler os pixels por script, sim.
    sam_image_source: Literal["api", "bucket"] = "api"

    # Redes dos proxies reversos que terminam TLS na frente da API (CIDR).
    # Só destes endereços o X-Forwarded-For é aceito — vazio significa "não há
    # proxy", e vale sempre o IP real da conexão. Preencher indevidamente
    # permite forjar o IP de origem e escapar do teto de login.
    # Ex.: VISIONYARD_TRUSTED_PROXIES='["10.0.0.0/8"]'
    trusted_proxies: list[str] = []


settings = Settings()
