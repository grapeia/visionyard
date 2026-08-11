"""Configuração por ambiente. Nada de segredo com default utilizável."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VISIONYARD_", env_file=".env",
                                      extra="ignore")

    database_url: str = "postgresql://postgres:postgres@localhost:5432/visionyard"

    # Cookie de sessão. secure=False só serve pra dev em http://localhost;
    # em produção o proxy termina TLS e isto vira True.
    cookie_name: str = "visionyard_session"
    cookie_secure: bool = False
    cookie_samesite: str = "lax"

    # Sessão expira em 12h de inatividade ou 7 dias absolutos — o teto absoluto
    # existe pra que uma sessão em uso contínuo não viva pra sempre.
    session_idle_hours: int = 12
    session_max_days: int = 7

    password_min_length: int = 12

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

    # Job cujo agente não bate ponto há este tempo é considerado órfão (reboot,
    # crash) e volta a ser reclamável por outro agente, respeitando max_attempts.
    # Precisa ser bem maior que o intervalo de heartbeat do agente, senão um
    # treino lento seria reclamado por engano e rodaria duas vezes.
    agent_job_stale_seconds: int = 180

    # Redes dos proxies reversos que terminam TLS na frente da API (CIDR).
    # Só destes endereços o X-Forwarded-For é aceito — vazio significa "não há
    # proxy", e vale sempre o IP real da conexão. Preencher indevidamente
    # permite forjar o IP de origem e escapar do teto de login.
    # Ex.: VISIONYARD_TRUSTED_PROXIES='["10.0.0.0/8"]'
    trusted_proxies: list[str] = []


settings = Settings()
