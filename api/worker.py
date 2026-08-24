"""Quem executa a importação — dentro da API ou como processo próprio.

    .venv/bin/python -m api.worker

É o mesmo código nos dois lugares. A diferença é só onde o laço roda:
`import_worker_embedded=true` (o default) o mantém dentro do processo que
atende as telas, que é o comportamento histórico; `false` o tira de lá, e
então este módulo precisa estar no ar como processo separado.

Por que não é um agente. O executor precisa enxergar `import_root` — o dump do
Label Studio e as imagens dele estão no disco de quem serve a API. Uma máquina
de GPU ou de borda não alcança esse caminho, e fazê-la alcançar seria expor o
diretório de importação na rede. Por isso a importação não passa por
`contracts/jobs.py`, que descreve o que atravessa máquina, e por isso
`services/agents.py: TIPOS_LOCAIS` mantém o claim do agente longe dela.

O laço embutido é acordado por quem enfileira (`cutucar`), então começar uma
importação continua sendo imediato como era com `asyncio.create_task`. O
`import_worker_poll_seconds` governa o worker de fora e o recolhimento
periódico, não essa latência.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable

from fastapi import HTTPException

from . import db, logs
from .config import settings
from .importers.base import ImportReport
from .importers.runner import run_import
from .services import imports as svc

logger = logging.getLogger(__name__)

# Despertador de cada laço vivo. É um conjunto, e não um `Event` de módulo,
# porque `asyncio.Event` se amarra ao loop em que foi esperado pela primeira
# vez: um só, criado no import, morreria com "bound to a different event loop"
# no segundo loop do processo — que é o que a suíte faz a cada teste.
_despertadores: set[asyncio.Event] = set()


def cutucar() -> None:
    """Avisa o laço embutido de que há trabalho — sem isso ele só olharia a
    fila no próximo poll, e enfileirar deixaria de ser imediato."""
    for despertador in _despertadores:
        despertador.set()


async def executar(job: dict) -> None:
    """Roda uma importação já reclamada, do começo ao desfecho.

    Todo o contexto sai da linha do job: `project_id` para o schema, `payload`
    para o importador, `created_by` para a autoria das anotações importadas e
    `data_source_id` para a origem. É o que permite executá-la noutro processo
    — não sobra nada preso à requisição que a pediu.
    """
    job_id = str(job["id"])
    report = ImportReport()
    try:
        ctx = await svc.contexto_do_projeto(str(job["project_id"]))
        importador = svc.construir_importador(job["payload"])
        await svc.registrar_evento(job_id,
                                   f"Importando de {importador.describe()}")
        await run_import(
            project_id=ctx["project_id"], schema_id=ctx["schema_id"],
            schema_classes=ctx["classes"], importer=importador,
            actor_id=str(job["created_by"]),
            class_overrides=job["payload"].get("class_overrides"),
            source_id=str(job["data_source_id"]), dry_run=False, report=report,
            progress_cb=lambda n: svc.registrar_progresso(job_id, n))
        await svc.concluir_job(job_id, report.as_dict())
        await svc.registrar_evento(
            job_id,
            f"Concluído: {report.samples_created} imagens novas, "
            f"{report.samples_existing} já existentes, "
            f"{report.annotations_created} anotações, "
            f"{report.skipped_total} puladas")
    except HTTPException as e:
        # Texto de serviço já é mensagem de usuário em português. Chega aqui
        # quando o payload deixou de valer entre enfileirar e executar — o caso
        # real é `import_root` diferente no processo que executa.
        logger.warning("importação %s recusada ao executar: %s", job_id,
                       e.detail)
        await svc.falhar_job(job_id, str(e.detail), report.as_dict())
        await svc.registrar_evento(job_id, str(e.detail), "error")
    except Exception as e:
        # `job.error` e os eventos são lidos com papel `viewer`. Erro do GCS ou
        # do banco cita conta de serviço, projeto e caminho interno — detalhe
        # fica no log, mesmo tratamento já aplicado na navegação de bucket.
        logger.exception("job de importação %s falhou", job_id)
        await svc.falhar_job(job_id, f"falhou: {type(e).__name__}",
                             report.as_dict())
        await svc.registrar_evento(job_id, f"Falhou ({type(e).__name__}) — "
                                           f"detalhe no log do servidor",
                                   "error")


async def passo() -> bool:
    """Reclama e executa uma importação. `False` quando a fila estava vazia.

    Uma volta do laço, exposta à parte para que o teste possa executar a fila
    sem depender de tempo — é a diferença entre afirmar "quem reclama executa"
    e esperar um laço fazer isso.
    """
    job = await svc.reclamar()
    if job is None:
        return False
    await executar(job)
    return True


async def lacar(parar: asyncio.Event,
                ativo: Callable[[], bool] = lambda: True) -> None:
    """Drena a fila até `parar` ser levantado.

    `ativo` é consultado a cada volta, e não uma vez na subida: o laço embutido
    passa `lambda: settings.import_worker_embedded`, e é por isso que desligá-lo
    não exige derrubar a API — e que o teste consegue observar a fila parada.
    """
    despertador = asyncio.Event()
    _despertadores.add(despertador)
    try:
        while not parar.is_set():
            try:
                if ativo():
                    while await passo():
                        if parar.is_set():
                            return
                    await svc.recolher_orfaos()
            except Exception:
                # Um erro aqui é do próprio laço (banco fora do ar, por
                # exemplo), não da importação — `executar` já fecha o job por
                # conta dela. Se subisse, o laço morreria calado e a fila
                # pararia para sempre.
                logger.exception("laço de importação falhou; segue tentando")
            await _dormir(despertador, settings.import_worker_poll_seconds)
    finally:
        _despertadores.discard(despertador)


async def _dormir(despertador: asyncio.Event, segundos: float) -> None:
    try:
        await asyncio.wait_for(despertador.wait(), timeout=segundos)
    except TimeoutError:
        pass
    finally:
        despertador.clear()


async def principal() -> None:
    """O worker de fora: sobe o pool e drena a fila até receber sinal.

    Ele não consulta `import_worker_embedded` — quem o subiu já decidiu que
    esta máquina executa importação. A configuração diz respeito só ao laço que
    mora dentro da API.
    """
    logs.configurar()
    await db.open_pool()
    parar = asyncio.Event()
    loop = asyncio.get_running_loop()
    for nome in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, nome), parar.set)
        except (NotImplementedError, AttributeError):
            pass        # Windows: cai no KeyboardInterrupt do asyncio.run
    logger.info("worker de importação no ar (poll %.1fs)",
                settings.import_worker_poll_seconds)
    try:
        await lacar(parar)
    finally:
        await db.close_pool()


if __name__ == "__main__":
    try:
        asyncio.run(principal())
    except KeyboardInterrupt:
        pass
