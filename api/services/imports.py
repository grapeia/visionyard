"""A fila da importação: enfileirar, reclamar, bater ponto, fechar.

A importação é a única execução do sistema que **não** atravessa máquina: ela
lê o dump e as imagens sob `import_root`, no disco da API, e um dispositivo de
borda não enxerga esse caminho. Por isso ela não é job de agente e não passa
por `contracts/jobs.py` — quem a executa é `api/worker.py`, o mesmo código
rodando ou dentro do processo da API ou como `python -m api.worker`.

O que a fila resolve é a diferença entre *pedir* e *executar*. Antes, quem
atendia o POST executava a importação ali mesmo (`asyncio.create_task`), e daí
saíam as três consequências do item 5 do PLANO: deploy no meio matava a
execução, o hash de um arquivo grande disputava o event loop com quem estava
atendendo requisição, e não havia como ter mais de uma réplica. Com o job
nascendo `queued` e sendo reclamado por `FOR UPDATE SKIP LOCKED`, os três
somem juntos: quem executa pode estar noutro processo, e o job que ninguém
pegou continua na fila para o próximo.

O recolhimento continua: quem parou de bater ponto é fechado como falha, numa
leitura de tela — é exatamente quando o job preso é visto, e numa instalação
sem agente nenhum nada mais roda periodicamente para fazê-lo.
"""
from __future__ import annotations

import json
from pathlib import Path as FsPath
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

from .. import db
from ..audit import audit
from ..auth import Principal
from ..config import settings
from ..importers.base import Importer, ImportReport
from ..importers.gcs_prefix import GCSPrefixImporter
from ..importers.label_studio import LabelStudioImporter
from ..importers.label_studio_json import LabelStudioJSONImporter
from ..importers.labelme import LabelMeImporter
from .datasets import travar_origens
from .projects import projeto_ou_404 as _projeto_ou_404

# Recolher deixa a mensagem na tela de quem importou, que a lê com papel
# `viewer`: ela diz o que aconteceu e o que fazer. Reimportar é seguro — a
# identidade da imagem é o md5, então o que já entrou não duplica.
MOTIVO_ORFAO = ("importação interrompida antes de terminar (o processo que a "
                "executava caiu ou reiniciou) — pode reimportar: o que já "
                "entrou não duplica")

# O outro jeito de a importação não acontecer, e que só existe desde que ela
# saiu do processo do POST: ninguém veio buscá-la. Diz o que fazer a quem
# administra sem mandar o usuário reimportar — reimportar não conserta fila
# sem executor.
MOTIVO_SEM_EXECUTOR = ("importação ficou na fila e ninguém veio executá-la — "
                       "o processo de importação (`python -m api.worker`) não "
                       "está no ar; avise quem administra a instalação")


async def recolher_orfaos() -> int:
    """Fecha as importações que não vão terminar. Devolve quantas.

    Dois casos, com mensagens diferentes porque a ação de quem lê é diferente:
    a que começou e parou de bater ponto (reimportar resolve), e a que nunca
    começou porque não há executor no ar (reimportar não resolve).

    O segundo caso é o delicado: fila que anda não está abandonada, e recolhê-la
    faria a terceira de três importações grandes morrer por ter esperado a vez.
    A prova de vida é dupla, e as duas metades são necessárias — **estar**
    executando uma agora, ou **ter terminado** uma há pouco:

    - só `running` deixava dois buracos. Um é o intervalo entre concluir uma e
      reclamar a próxima, em que nenhuma está `running` e uma tela de jobs
      aberta nesse instante recolheria a fila inteira. O outro é pior: o UPDATE
      logo acima acaba de tirar do `running`, na mesma transação, justamente a
      que passou do prazo — e um dump grande passa minutos numa imagem só sem
      bater ponto, com o executor vivo do outro lado.
    - `finished_at` recente fecha os dois: quem terminou alguma coisa há menos
      de um prazo estava no ar há menos de um prazo.

    A prova é da instalação, e não do projeto, de propósito: o executor é um só
    para todos os projetos, então é a liveness dele que se está medindo.
    """
    async with db.connection() as conn, conn.transaction():
        mortas = await db.fetch_all(
            "UPDATE job SET status = 'failed', finished_at = now(),"
            "               error = %(erro)s"
            " WHERE type = 'import' AND status = 'running'"
            "   AND COALESCE(heartbeat_at, started_at, created_at)"
            "       < now() - make_interval(secs => %(s)s)"
            " RETURNING id",
            {"erro": MOTIVO_ORFAO,
             "s": settings.import_job_stale_seconds}, conn=conn)

        paradas = await db.fetch_all(
            "UPDATE job SET status = 'failed', finished_at = now(),"
            "               error = %(erro)s"
            " WHERE type = 'import' AND status = 'queued'"
            "   AND created_at < now() - make_interval(secs => %(s)s)"
            "   AND NOT EXISTS (SELECT 1 FROM job f"
            "                    WHERE f.type = 'import'"
            "                      AND (f.status = 'running'"
            "                           OR f.finished_at > now()"
            "                              - make_interval(secs => %(s)s)))"
            " RETURNING id",
            {"erro": MOTIVO_SEM_EXECUTOR,
             "s": settings.import_job_stale_seconds}, conn=conn)

        for job_id, motivo in ([(m["id"], MOTIVO_ORFAO) for m in mortas]
                               + [(q["id"], MOTIVO_SEM_EXECUTOR)
                                  for q in paradas]):
            await registrar_evento(str(job_id), motivo, "error", conn=conn)
    return len(mortas) + len(paradas)


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
    async with db.connection() as conn:
        await db.execute(
            "UPDATE job SET progress = %s::jsonb, heartbeat_at = now()"
            " WHERE id = %s AND status = 'running'",
            (json.dumps({"lidos": lidos}), job_id), conn=conn)


async def concluir_job(job_id: str, resultado: dict[str, Any]) -> None:
    """Fecha com sucesso — se o job ainda não tiver desfecho.

    O `AND status = 'running'` **é** o controle: a task pode ter sobrevivido ao
    reaper (o processo não morreu, só a importação passou do prazo sem bater
    ponto) e terminar depois dele. Sem a guarda, ela reescreveria como
    `succeeded` um job que a tela já mostrou como interrompido — a mesma
    invariante do relato atrasado do agente.
    """
    async with db.connection() as conn:
        await db.execute(
            "UPDATE job SET status = 'succeeded', finished_at = now(),"
            "               result = %s::jsonb"
            " WHERE id = %s AND status = 'running'",
            (json.dumps(resultado), job_id), conn=conn)


async def falhar_job(job_id: str, erro: str,
                     resultado: dict[str, Any]) -> None:
    """Fecha com falha, com a mesma guarda de `concluir_job`."""
    async with db.connection() as conn:
        await db.execute(
            "UPDATE job SET status = 'failed', finished_at = now(),"
            "               error = %s, result = %s::jsonb"
            " WHERE id = %s AND status = 'running'",
            (erro, json.dumps(resultado), job_id), conn=conn)


# --------------------------------------------------------------------------- #
# A origem, o projeto e o importador — o que o router fazia inline
# --------------------------------------------------------------------------- #

def _dentro_da_raiz(caminho: str) -> FsPath:
    """Resolve o caminho e exige que ele esteja sob `settings.import_root`.

    `resolve()` antes da comparação porque `../` e symlink escapariam de uma
    checagem textual.
    """
    raiz = FsPath(settings.import_root).resolve()
    alvo = FsPath(caminho).resolve()
    if raiz != alvo and raiz not in alvo.parents:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"caminho fora da pasta de importação ({settings.import_root})")
    return alvo


def construir_importador(payload: dict[str, Any]) -> Importer:
    """O importador descrito pelo payload — o **único** leitor dessa forma.

    Ele é lido duas vezes e em processos diferentes: no POST, para validar a
    entrada e descrever a origem, e depois pelo executor, que relê a mesma
    linha de `job.payload`. Ter um leitor só é o que impede a chave renomeada
    de passar no POST e falhar depois no executor.

    **Toda chave é lida com default**, pela mesma razão que vale para
    `contracts/jobs.py`: desde que a importação virou fila, um job enfileirado
    antes de um deploy é reclamado depois dele, e o que atravessa é o payload,
    não o código. Campo novo no `ImportIn` lido aqui com `[]` mataria com
    `KeyError` tudo o que já estava na fila.

    A cerca de caminho é conferida nas duas leituras, e não só na primeira:
    `import_root` é do processo que executa, e o worker pode ter outro.
    """
    kind = payload.get("kind")
    if kind == "gcs_prefix":
        if not payload.get("bucket", "").strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "informe o bucket")
        return GCSPrefixImporter(bucket=payload["bucket"],
                                 prefix=payload.get("prefix", ""),
                                 limit=payload.get("limit"))
    if kind not in {"label_studio_sqlite", "label_studio_json", "labelme_json"}:
        # Payload que este processo não sabe ler. Acontece com job enfileirado
        # por um controlador mais novo — o mesmo caso do `PayloadInvalido` do
        # contrato do agente, e a mensagem tem de dizer o que fazer.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"origem de importação desconhecida ({kind!r}) — atualize o "
            f"processo que executa a importação")
    campo = "sqlite_path" if kind == "label_studio_sqlite" else "json_path"
    if not payload.get(campo):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "informe o caminho do dump ou JSON de importação")
    if not payload.get("image_root"):
        # Sem isto o importador pula TODOS os samples (não tem como calcular a
        # identidade) e o job termina com "succeeded" e zero importados.
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "informe onde estão as imagens (image_root)")
    caminho = _dentro_da_raiz(payload[campo])
    raiz_img = _dentro_da_raiz(payload["image_root"])
    try:
        if kind == "labelme_json":
            return LabelMeImporter(str(caminho), str(raiz_img),
                                   payload.get("image_base_uri", ""))
        if kind == "label_studio_json":
            return LabelStudioJSONImporter(str(caminho), str(raiz_img),
                                           payload.get("image_base_uri", ""))
        return LabelStudioImporter(
            sqlite_path=str(caminho), image_root=str(raiz_img),
            image_base_uri=payload.get("image_base_uri", ""),
            project_id=payload.get("ls_project_id"))
    except FileNotFoundError:
        # Mensagem sem o caminho: dizer "não encontrado em /x/y" confirmaria o
        # que existe e o que não existe no servidor.
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "arquivo de importação não encontrado") from None


async def contexto_do_projeto(project_id: str, *,
                             conn: db.Conn | None = None) -> dict[str, Any]:
    """Projeto + schema corrente + classes, que é o que `run_import` precisa.

    Resolvido por quem executa, e não gravado no payload: o executor pode ser
    outro processo, e o que ele tem em mãos é a linha do job.
    """
    # Todo projeto nasce com o schema v1 na mesma transação de `create_project`:
    # existe projeto sem imagem, nunca projeto sem schema.
    schema = await db.fetch_row(
        "SELECT id, classes FROM label_schema WHERE project_id = %s"
        " ORDER BY version DESC LIMIT 1", (project_id,), conn=conn)
    return {"project_id": project_id, "schema_id": str(schema["id"]),
            "classes": [c["name"] for c in schema["classes"]]}


async def upsert_source(project_id: str, kind: str, uri: str, name: str,
                        config: dict, actor_id: str, *,
                        conn: db.Conn | None = None) -> str:
    """Reaproveita a origem existente para o mesmo destino.

    Reimportar o mesmo prefixo não pode criar uma segunda origem, senão o
    mesmo bucket apareceria duas vezes na tela do dataset.
    """
    row = await db.fetch_row(
        "INSERT INTO data_source (project_id, kind, uri, name, config,"
        " created_by) VALUES (%s,%s,%s,%s,%s::jsonb,%s)"
        " ON CONFLICT (project_id, kind, uri) WHERE uri IS NOT NULL"
        " DO UPDATE SET name = EXCLUDED.name RETURNING id",
        (project_id, kind, uri, name, json.dumps(config), actor_id),
        conn=conn)
    return str(row["id"])


# --------------------------------------------------------------------------- #
# Fila
# --------------------------------------------------------------------------- #

async def enfileirar(actor: Principal, *, project_id: str, kind: str,
                     payload: dict[str, Any], source_id: str,
                     ip: str | None, conn: db.Conn) -> str:
    """Cria o job `queued` e o registra na auditoria, na mesma transação.

    Nasce `queued`, e não `running` como antes: quem o marca `running` é quem
    o reclama. Enquanto ninguém reclamou, ele está apenas esperando — e é isso
    que faz um deploy no meio deixar de matar a importação.
    """
    job = await db.fetch_row(
        "INSERT INTO job (project_id, type, payload, status, created_by,"
        " data_source_id)"
        " VALUES (%s,'import',%s::jsonb,'queued',%s,%s) RETURNING id",
        (project_id, json.dumps(payload), actor.id, source_id), conn=conn)
    job_id = str(job["id"])
    await audit(actor, "import.start", "job", job_id, project_id=project_id,
                after={"kind": kind}, ip=ip, conn=conn)
    return job_id


async def reclamar() -> dict[str, Any] | None:
    """Pega a próxima importação da fila, ou `None`. Chamada em laço.

    `FOR UPDATE SKIP LOCKED` pela mesma razão do claim do agente: dois
    executores rodando ao mesmo tempo — duas réplicas da API, ou a API e um
    worker de fora — nunca pegam o mesmo job, e nenhum dos dois espera pelo
    outro.

    Só pega `queued`. Importação que travou no meio não volta para a fila: o
    recolhimento a fecha como falha, e refazê-la é decisão de quem lê a tela —
    é o mesmo desfecho de antes, e mantê-lo aqui é o que impede esta mudança
    de virar também uma mudança de política de retentativa.
    """
    async with db.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "UPDATE job SET status = 'running', started_at = now(),"
            "               heartbeat_at = now(), attempts = attempts + 1"
            " WHERE id = (SELECT id FROM job"
            "              WHERE type = 'import' AND status = 'queued'"
            "                AND NOT cancel_requested"
            "              ORDER BY priority, created_at"
            "              FOR UPDATE SKIP LOCKED LIMIT 1)"
            " RETURNING id, project_id, payload, created_by,"
            "           data_source_id")
        return await cur.fetchone()


async def simular(project_slug: str, actor: Principal,
                  payload: dict[str, Any]) -> dict[str, Any]:
    """O relatório do que a importação faria, sem gravar nada.

    É o modo default da rota de propósito: importar mexe no dado de todo mundo,
    e o custo de conferir o mapeamento de classes antes é uma requisição.
    """
    from ..importers.runner import run_import

    async with db.connection() as conn:
        proj = await _projeto_ou_404(conn, project_slug)
        ctx = await contexto_do_projeto(str(proj["id"]), conn=conn)
    importador = construir_importador(payload)
    report = ImportReport()
    await run_import(project_id=ctx["project_id"], schema_id=ctx["schema_id"],
                     schema_classes=ctx["classes"], importer=importador,
                     actor_id=actor.id,
                     class_overrides=payload.get("class_overrides"),
                     source_id=None, dry_run=True, report=report)
    return {"dry_run": True, "source": importador.describe(),
            "source_classes": importador.classes(),
            "schema_classes": ctx["classes"], "report": report.as_dict()}


async def pedir(project_slug: str, actor: Principal, payload: dict[str, Any],
                ip: str | None) -> dict[str, Any]:
    """Registra a origem e enfileira. **Não executa** — quem executa é
    `api/worker.py`, reclamando da fila.

    O importador é construído aqui só para validar a entrada (caminho fora de
    `import_root`, dump inexistente, bucket em branco) e descrever a origem na
    resposta; quem o constrói de novo para rodar é o executor, a partir da
    mesma linha de `job.payload`.
    """
    importador = construir_importador(payload)
    async with db.connection() as conn, conn.transaction():
        proj = await _projeto_ou_404(conn, project_slug)
        # Não enfileira por baixo de uma remoção de origem em curso, que
        # acabou de conferir que não há importação desta origem na fila.
        await travar_origens(conn, str(proj["id"]))
        gcs = payload["kind"] == "gcs_prefix"
        uri = (f"gs://{payload['bucket'].strip('/')}"
               f"/{payload.get('prefix', '').lstrip('/')}"
               if gcs else payload.get(
                   "sqlite_path" if payload["kind"] == "label_studio_sqlite"
                   else "json_path", ""))
        source_id = await upsert_source(
            str(proj["id"]), "gcs" if gcs else "import", uri,
            importador.describe(), payload, actor.id, conn=conn)
        job_id = await enfileirar(
            actor, project_id=str(proj["id"]), kind=payload["kind"],
            payload=payload, source_id=source_id, ip=ip, conn=conn)
    # Devolve a origem criada/reaproveitada: quem chamou precisa vinculá-la a
    # um dataset, e adivinhar "a mais recente" depois seria uma corrida.
    return {"dry_run": False, "job_id": job_id, "status": "queued",
            "source_id": source_id, "source": importador.describe()}


# --------------------------------------------------------------------------- #
# As telas de job do projeto
# --------------------------------------------------------------------------- #

async def listar_jobs(project_slug: str, tipo: str | None,
                      limite: int) -> list[dict[str, Any]]:
    """Jobs do projeto, do mais recente ao mais antigo.

    Recolhe antes de listar: importação que parou de bater ponto ficaria em
    `running` para sempre, e esta tela é onde ela é vista. É escrita num GET de
    propósito — não há nada periódico no processo da API quando o laço embutido
    está desligado, e numa instalação sem agente nem o reaper do claim roda.
    """
    await recolher_orfaos()
    async with db.connection() as conn:
        return await db.fetch_all(
            "SELECT j.id, j.type, j.status, j.progress, j.result, j.error,"
            "       j.created_at, j.finished_at, u.email AS created_by"
            "  FROM job j JOIN project p ON p.id = j.project_id"
            "  LEFT JOIN app_user u ON u.id = j.created_by"
            " WHERE p.slug = %s AND (%s::text IS NULL OR j.type = %s)"
            " ORDER BY j.created_at DESC LIMIT %s",
            (project_slug, tipo, tipo, min(limite, 100)), conn=conn)


async def detalhe_do_job(project_slug: str, job_id: UUID) -> dict[str, Any]:
    async with db.connection() as conn:
        job = await db.fetch_one(
            "SELECT j.id, j.type, j.status, j.progress, j.result, j.error,"
            "       j.created_at, j.started_at, j.finished_at"
            "  FROM job j JOIN project p ON p.id = j.project_id"
            " WHERE p.slug = %s AND j.id = %s", (project_slug, job_id),
            conn=conn)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "job não encontrado")
        eventos = await db.fetch_all(
            "SELECT seq, at, level, message FROM job_event"
            " WHERE job_id = %s ORDER BY seq", (job_id,), conn=conn)
    return {**job, "events": eventos}
