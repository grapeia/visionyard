"""Loop do agente: registra-se, reclama jobs e executa.

    python -m agent.main

Detecta as próprias capacidades e as anuncia; as flags de roteamento
(`train`/`infer`) vêm do cadastro do device (o admin decide quem faz o quê), o
agente só acrescenta o diagnóstico (GPU, CUDA). Um job por vez: o loop bloqueia
no treino e só volta a reclamar quando termina — é uma máquina com uma só GPU,
fila serial é o comportamento correto.
"""
from __future__ import annotations

import platform
import time
import traceback

from .client import ApiError, Client
from .config import Config

AGENT_VERSION = "0.1.0"


def detectar_capacidades() -> dict:
    """Diagnóstico da máquina — complementa (não substitui) o que o admin
    cadastrou. Torch é opcional: um agente de inferência pode não tê-lo."""
    caps: dict = {"host": platform.node(), "os": platform.platform()}
    try:
        import torch
        caps["torch"] = torch.__version__
        if torch.cuda.is_available():
            caps["gpu"] = torch.cuda.get_device_name(0)
            caps["cuda"] = torch.version.cuda
    except Exception:
        pass
    return caps


def executar_job(cli: Client, cfg: Config, job: dict) -> None:
    from pathlib import Path
    tipo = job["type"]
    # Importado com apelido: os quatro executores se chamam `executar`, e sob o
    # mesmo nome o mypy vê uma redefinição com outra assinatura em cada ramo.
    if tipo == "train":
        from .train import executar as treinar
        treinar(cli, job, Path(cfg.work_dir), cfg.models_bucket,
                cfg.models_prefix)
    elif tipo == "prelabel":
        from .prelabel import executar as pretaguear
        pretaguear(cli, job, Path(cfg.work_dir))
    elif tipo == "export":
        from .export import executar as compilar
        compilar(cli, job, Path(cfg.work_dir), cfg.models_bucket,
                 cfg.models_prefix)
    elif tipo == "evaluate":
        from .evaluate import executar as avaliar
        avaliar(cli, job, Path(cfg.work_dir))
    else:
        # tipo desconhecido: recusa limpa em vez de deixar o job preso em
        # 'running' para sempre.
        cli.finish(job["id"], "failed",
                   error=f"tipo de job '{tipo}' ainda não suportado por este agente")


def processar_deployments(cli: Client, cfg: Config) -> None:
    """Puxa os builds pendentes para esta máquina e os coloca no caminho que o
    coletor lê. Ativar troca o link `current` — a implantação anterior é
    superseded no controlador."""
    from pathlib import Path

    from . import gcs
    for dep in cli.pending_deployments():
        try:
            destino = (Path(cfg.deploy_dir) / dep["model_slug"])
            destino.mkdir(parents=True, exist_ok=True)
            arquivo = destino / f"v{dep['version']}.{dep['format']}"
            gcs.baixar(dep["uri"], arquivo)
            # `current`: o coletor lê sempre este nome; trocar aqui é a ativação.
            # Troca ATÔMICA (symlink temporário + os.replace) para o coletor
            # nunca ver o link ausente no meio da troca.
            import os
            link = destino / f"current.{dep['format']}"
            try:
                tmp = destino / f".current.{dep['format']}.novo"
                if tmp.exists() or tmp.is_symlink():
                    tmp.unlink()
                tmp.symlink_to(arquivo.name)
                os.replace(tmp, link)
            except OSError:
                import shutil
                shutil.copy2(arquivo, link)
            print(f"deploy {dep['id']}: {dep['model_slug']} v{dep['version']} "
                  f"({dep['format']}) ativo em {link}")
            cli.deployment_state(dep["id"], "active")
        except Exception as e:      # falha de um deploy não derruba o agente
            print(f"deploy {dep['id']} falhou: {e}")
            try:
                cli.deployment_state(dep["id"], "failed", str(e)[:500])
            except ApiError:
                pass


def main() -> int:
    cfg = Config.from_env()
    cli = Client(cfg.api_url, cfg.token)
    caps = detectar_capacidades()
    reg = cli.register(caps, AGENT_VERSION)
    print(f"agente '{reg['slug']}' registrado em {cfg.api_url}; capacidades: {caps}")

    ultimo_hb = 0.0
    while True:
        try:
            # deployments são device-scoped: processa antes de reclamar job
            processar_deployments(cli, cfg)
            job = cli.claim()
            if job is None:
                agora = time.monotonic()
                if agora - ultimo_hb >= cfg.heartbeat_seconds:
                    cli.device_heartbeat("idle")
                    ultimo_hb = agora
                time.sleep(cfg.poll_seconds)
                continue

            print(f"job {job['id']} ({job['type']}) — tentativa {job.get('attempt')}")
            try:
                executar_job(cli, cfg, job)
                print(f"job {job['id']} concluído")
            except ApiError:
                raise
            except Exception as e:      # falha do job, não do agente
                print(f"job {job['id']} falhou: {e}")
                traceback.print_exc()
                try:
                    cli.finish(job["id"], "failed", error=str(e)[:2000])
                except ApiError:
                    pass
            ultimo_hb = 0.0        # força heartbeat no próximo ciclo ocioso
        except ApiError as e:
            # erro de comunicação com o controlador: espera e tenta de novo,
            # sem derrubar o agente (o controlador pode ter reiniciado)
            print(f"erro de API: {e}")
            time.sleep(cfg.poll_seconds * 2)
        except KeyboardInterrupt:
            print("encerrando")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
