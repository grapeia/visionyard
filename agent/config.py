"""Configuração do agente, por variável de ambiente.

Roda em servidores de GPU (capacidade `train`) e em dispositivos de
borda (`infer`). Nada de
segredo com default utilizável: sem API_URL e AGENT_TOKEN o agente recusa subir.

    VISIONYARD_API_URL=http://api.interna:8000
    VISIONYARD_AGENT_TOKEN=<token do device, emitido em Devices>

Não há credencial de nuvem aqui: o bucket é lido e gravado por URL que a API
assina por job, e o caminho onde o peso é publicado também é ela que escolhe
(`VISIONYARD_MODELS_BUCKET` agora é configuração da API).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    api_url: str
    token: str
    work_dir: str
    deploy_dir: str             # onde o build ativo é colocado p/ o coletor ler
    poll_seconds: float
    heartbeat_seconds: float

    @classmethod
    def from_env(cls) -> Config:
        api = os.environ.get("VISIONYARD_API_URL", "").rstrip("/")
        token = os.environ.get("VISIONYARD_AGENT_TOKEN", "")
        if not api or not token:
            raise SystemExit(
                "defina VISIONYARD_API_URL e VISIONYARD_AGENT_TOKEN antes de "
                "subir o agente")
        return cls(
            api_url=api,
            token=token,
            work_dir=os.environ.get("VISIONYARD_WORK_DIR", "./agent-work"),
            deploy_dir=os.environ.get("VISIONYARD_DEPLOY_DIR", "./visionyard-deploy"),
            poll_seconds=float(os.environ.get("VISIONYARD_POLL_SECONDS", "5")),
            heartbeat_seconds=float(
                os.environ.get("VISIONYARD_HEARTBEAT_SECONDS", "30")),
        )
