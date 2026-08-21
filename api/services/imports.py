"""Ciclo de vida do job de importação.

A importação roda dentro do processo da API (`asyncio.create_task`, em
`routers/imports.py`), e é o que a torna a única execução do sistema sem dono
do outro lado: deploy no meio leva a task junto e a linha do job fica em
`running` para sempre. O reaper do agente não a alcança — ele exige
`heartbeat_at` e `attempts >= max_attempts`, e importação não batia ponto nem
conta tentativa.

Enquanto a execução não sai do processo (item 5 do PLANO), o que fecha o buraco
é um par: a importação bate ponto enquanto lê, e quem abre a tela de jobs
recolhe o que parou de bater. Recolher numa leitura é de propósito — é
exatamente quando o job preso é visto, e numa instalação sem agente nenhum
nada mais roda periodicamente para fazê-lo.
"""
from __future__ import annotations

import json
from typing import Any

from .. import db
from ..config import settings

# Recolher deixa a mensagem na tela de quem importou, que a lê com papel
# `viewer`: ela diz o que aconteceu e o que fazer. Reimportar é seguro — a
# identidade da imagem é o md5, então o que já entrou não duplica.
MOTIVO_ORFAO = ("importação interrompida antes de terminar (o processo da API "
                "caiu ou reiniciou) — pode reimportar: o que já entrou não "
                "duplica")


async def recolher_orfaos() -> int:
    """Fecha as importações que pararam de bater ponto. Devolve quantas."""
    async with db.connection() as conn:
        async with conn.transaction():
            mortas = await db.fetch_all(
                "UPDATE job SET status = 'failed', finished_at = now(),"
                "               error = %(erro)s"
                " WHERE type = 'import' AND status = 'running'"
                "   AND COALESCE(heartbeat_at, started_at, created_at)"
                "       < now() - make_interval(secs => %(s)s)"
                " RETURNING id",
                {"erro": MOTIVO_ORFAO,
                 "s": settings.import_job_stale_seconds}, conn=conn)
            for m in mortas:
                await registrar_evento(str(m["id"]), MOTIVO_ORFAO, "error",
                                       conn=conn)
    return len(mortas)


async def registrar_evento(job_id: str, mensagem: str, nivel: str = "info",
                           *, conn: db.Conn | None = None) -> None:
    """Linha no diário do job — é o que a tela de detalhe mostra.

    `ON CONFLICT DO NOTHING` porque há dois escritores desde que o
    recolhimento passou a registrar evento: ele e a task que ainda pode estar
    viva (o caso que a guarda de `concluir_job` prevê). O seq sai de
    `max(seq)+1`, e duas conexões podem calcular o mesmo — a chave é
    (job_id, seq). Perder uma linha do diário é barato; deixar a violação subir
    abortaria a transação do recolhimento inteiro, e a tela de jobs que o
    dispara devolveria 500.
    """
    await db.execute(
        "INSERT INTO job_event (job_id, seq, level, message)"
        " SELECT %s, COALESCE(max(seq), 0) + 1, %s, %s FROM job_event"
        " WHERE job_id = %s"
        " ON CONFLICT (job_id, seq) DO NOTHING",
        (job_id, nivel, mensagem, job_id), conn=conn)


async def registrar_progresso(job_id: str, lidos: int) -> None:
    """Progresso e ponto na mesma escrita: o progresso já ia ao banco a cada
    lote, e é ele que prova que a importação continua viva."""
    await db.execute(
        "UPDATE job SET progress = %s::jsonb, heartbeat_at = now()"
        " WHERE id = %s AND status = 'running'",
        (json.dumps({"lidos": lidos}), job_id))


async def concluir_job(job_id: str, resultado: dict[str, Any]) -> None:
    """Fecha com sucesso — se o job ainda não tiver desfecho.

    O `AND status = 'running'` **é** o controle: a task pode ter sobrevivido ao
    reaper (o processo não morreu, só a importação passou do prazo sem bater
    ponto) e terminar depois dele. Sem a guarda, ela reescreveria como
    `succeeded` um job que a tela já mostrou como interrompido — a mesma
    invariante do relato atrasado do agente.
    """
    await db.execute(
        "UPDATE job SET status = 'succeeded', finished_at = now(),"
        "               result = %s::jsonb"
        " WHERE id = %s AND status = 'running'",
        (json.dumps(resultado), job_id))


async def falhar_job(job_id: str, erro: str,
                     resultado: dict[str, Any]) -> None:
    """Fecha com falha, com a mesma guarda de `concluir_job`."""
    await db.execute(
        "UPDATE job SET status = 'failed', finished_at = now(), error = %s,"
        "               result = %s::jsonb"
        " WHERE id = %s AND status = 'running'",
        (erro, json.dumps(resultado), job_id))
