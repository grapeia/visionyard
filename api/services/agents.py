"""Protocolo do agente: a regra por trás de cada passo do ciclo de execução.

    register → claim → (manifest) → heartbeat/events/metrics* → model → finish

O *claim* é uma corrida entre agentes resolvida no banco: `FOR UPDATE SKIP
LOCKED` garante que dois agentes nunca peguem o mesmo job, e `capabilities @>
requires` garante que só uma máquina compatível o pegue (uma máquina de GPU pega
`train`, um dispositivo de inferência não). Job cujo dono morreu — heartbeat
velho — volta a ser reclamável, para que um reboot não deixe treino preso para
sempre.

Toda função aqui recebe o `device_id` já autenticado: quem valida o
`X-Agent-Token` é o router. O que estas funções garantem é a outra metade — que
a máquina só mexa no que é dela (`job_da_maquina`) e só dentro do projeto do job
(o `WHERE` de `gravar_prelabels`).
"""
from __future__ import annotations

import json
import math
from pathlib import Path as FsPath
from typing import Any

from fastapi import HTTPException, status

from contracts import jobs

from .. import db
from ..config import settings

# Chaves que o agente pode declarar: só DIAGNÓSTICO. As flags de roteamento
# (train/infer) são do admin — se o agente pudesse mesclá-las, um dispositivo
# cadastrado como `infer` se declararia `train` e passaria a reclamar treino,
# furando o controle de quem executa o quê. jsonb `||` faz a direita vencer,
# então filtrar a entrada é o que impede a escalada (não a ordem do merge).
CAPS_DIAGNOSTICO = {"gpu", "cuda", "vram_gb", "jetpack", "dla", "trt",
                    "torch", "host", "os", "driver", "arch"}


async def _device_do_token(device_id: str) -> dict[str, Any]:
    """Relê o device já autenticado, para as decisões que dependem do cadastro.

    O `device_id` vem do token, então a linha existe — exceto se um admin apagar
    a máquina entre a autenticação e esta consulta. Aí é 401 (o agente entende:
    para de tentar), e não o 500 de indexar `None`.
    """
    row = await db.fetch_one(
        "SELECT status, device_class FROM device WHERE id = %s", (device_id,))
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "device não existe mais")
    return row


# --------------------------------------------------------------------------- #
# Presença
# --------------------------------------------------------------------------- #

async def registrar_presenca(device_id: str, capabilities: dict) -> None:
    diag = {k: v for k, v in capabilities.items() if k in CAPS_DIAGNOSTICO}
    await db.execute(
        "UPDATE device SET capabilities = capabilities || %s::jsonb,"
        "  status = CASE WHEN status = 'disabled' THEN status ELSE 'idle' END,"
        "  last_heartbeat = now() WHERE id = %s",
        (json.dumps(diag), device_id))


async def bater_ponto(device_id: str, status_agente: str) -> None:
    await db.execute(
        "UPDATE device SET last_heartbeat = now(),"
        "  status = CASE WHEN status IN ('disabled','draining') THEN status"
        "                ELSE %s END WHERE id = %s", (status_agente, device_id))


# --------------------------------------------------------------------------- #
# Claim
# --------------------------------------------------------------------------- #

# Job que roda DENTRO do processo da API não é reclamável por máquina nenhuma:
# não há executor do outro lado, e o agente o encerraria como "tipo não
# suportado" por cima de quem o está executando. O claim casa `requires` vazio
# com qualquer capacidade e não filtra por tipo, então a exclusão precisa ser
# explícita — foi o que faltou quando a importação passou a bater ponto e virou
# alcançável. Quem cuida deles é `services/imports.py`.
TIPOS_LOCAIS = ["import"]


async def recolher_orfaos() -> None:
    """Leva a um estado terminal o job órfão que NÃO pode mais ser reclamado.

    O agente morreu no limite de tentativas, ou o job foi cancelado com o agente
    já fora do ar: `reclamar_job` só reclama attempts<max e !cancel_requested,
    então sem isto o job ficaria preso em 'running' para sempre.
    """
    await db.execute(
        "WITH mortos AS ("
        "  UPDATE job SET"
        "    status = CASE WHEN cancel_requested THEN 'canceled' ELSE 'failed' END,"
        "    error = COALESCE(error, 'agente parou de responder'),"
        "    finished_at = now()"
        "  WHERE status IN ('claimed','running')"
        "    AND type <> ALL(%(locais)s::text[])"
        "    AND heartbeat_at < now() - make_interval(secs => %(s)s)"
        "    AND (attempts >= max_attempts OR cancel_requested)"
        "  RETURNING id, status)"
        " UPDATE training_run r"
        "    SET status = CASE WHEN m.status = 'canceled' THEN 'stopped'"
        "                      ELSE 'failed' END, finished_at = now()"
        "   FROM mortos m WHERE r.job_id = m.id AND r.status = 'running'",
        {"s": settings.agent_job_stale_seconds, "locais": TIPOS_LOCAIS})


async def reclamar_job(device_id: str) -> dict | None:
    """Reclama o próximo job compatível; `None` quando não há nada para esta
    máquina. `draining`/`disabled` não reclamam — só terminam o que já pegaram,
    para o dispositivo de produção poder sair do pool sem largar um job no
    meio."""
    d = await _device_do_token(device_id)
    if d["status"] in ("draining", "disabled"):
        return None

    await recolher_orfaos()

    async with db.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                """
                UPDATE job SET status = 'claimed', device_id = %(dev)s,
                       claimed_at = now(), heartbeat_at = now(),
                       attempts = attempts + 1, error = NULL
                 WHERE id = (
                    SELECT j.id FROM job j
                     WHERE (SELECT capabilities FROM device WHERE id = %(dev)s)
                           @> j.requires
                       AND j.type <> ALL(%(locais)s::text[])
                       AND (j.target_device_id IS NULL
                            OR j.target_device_id = %(dev)s)
                       AND NOT j.cancel_requested
                       AND (
                         j.status = 'queued'
                         OR (j.status IN ('claimed','running')
                             AND j.heartbeat_at
                                 < now() - make_interval(secs => %(stale)s)
                             AND j.attempts < j.max_attempts)
                       )
                     ORDER BY (j.status = 'queued') DESC, j.priority, j.created_at
                     FOR UPDATE SKIP LOCKED
                     LIMIT 1)
                RETURNING id, type, payload, project_id, attempts
                """,
                {"dev": device_id, "locais": TIPOS_LOCAIS,
                 "stale": settings.agent_job_stale_seconds})
            job = await cur.fetchone()
            if job is None:
                return None
            await conn.execute(
                "UPDATE device SET status = 'busy' WHERE id = %s", (device_id,))
    return {"id": str(job["id"]), "type": job["type"],
            "payload": job["payload"], "attempt": job["attempts"]}


async def job_da_maquina(job_id: str, device_id: str) -> dict:
    """Job que ESTA máquina reclamou — outra não mexe no job alheio."""
    row = await db.fetch_one(
        "SELECT id, type, payload, status, device_id, cancel_requested,"
        "       project_id, created_by FROM job WHERE id = %s", (job_id,))
    if row is None or str(row["device_id"]) != device_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job não é desta máquina")
    return row


async def job_com_payload[P: jobs.Payload](
        job_id: str, device_id: str, contrato: type[P]) -> tuple[dict, P]:
    """O job desta máquina, com o payload já lido pelo contrato.

    Confere o TIPO antes de ler, e é essa conferência que faltava: até aqui,
    quem barrava um agente chamando `/build` num job de treino era o payload de
    treino não ter `model_version_id` — um 404 que saía por acidente do `.get()`
    e que sumiria ao ler pelo contrato. Em `/targets` nem 404 saía: o escopo
    lido com `.get()` virava `None`, a consulta não casava com nada e a resposta
    era 200 com lista vazia.

    Payload do tipo certo que o parser recusa é bug de quem enfileirou, não
    entrada do agente — sobe e cai na rede de `logs.correlacionar`, como 500 com
    o código da requisição no corpo e o traceback no log.
    """
    job = await job_da_maquina(job_id, device_id)
    _confere_tipo(job, contrato.TIPO)
    return job, contrato.de_payload(job["payload"])


def _confere_tipo(job: dict, *tipos: str) -> None:
    """404 antes de ler o payload — ver `job_com_payload`."""
    if job["type"] not in tipos:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"job é de {job['type']}, e esta rota é de {' ou '.join(tipos)}")


# --------------------------------------------------------------------------- #
# Manifesto (só quem é dono do job que o lê)
# --------------------------------------------------------------------------- #

async def diretorio_manifesto(job_id: str, device_id: str) -> FsPath:
    """O manifesto tem dois leitores: o treino, que aprende com ele, e a
    avaliação, que mede contra ele. Por isso esta é a única rota do agente que
    não pertence a um tipo só — exigir `Train` aqui devolve 404 para toda
    avaliação, antes de ela baixar o benchmark."""
    job = await job_da_maquina(job_id, device_id)
    _confere_tipo(job, jobs.Train.TIPO, jobs.Evaluate.TIPO)
    contrato: type[jobs.Train] | type[jobs.Evaluate] = (
        jobs.Train if job["type"] == jobs.Train.TIPO else jobs.Evaluate)
    p = contrato.de_payload(job["payload"])
    row = await db.fetch_one(
        "SELECT manifest_uri FROM dataset_version WHERE id = %s",
        (p.dataset_version_id,))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "versão sem manifesto")
    return FsPath(row["manifest_uri"])


def arquivo_do_manifesto(diretorio: FsPath, nome: str) -> FsPath:
    caminho = diretorio / nome
    if not caminho.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{nome} ausente")
    return caminho


# --------------------------------------------------------------------------- #
# Progresso
# --------------------------------------------------------------------------- #

async def atualizar_progresso(job_id: str, device_id: str,
                              progress: dict) -> dict:
    """Bate ponto do job e devolve se foi pedido cancelamento — é como o agente
    sabe que precisa parar (a rota de usuário só levanta a flag)."""
    row = await db.fetch_one(
        "UPDATE job SET heartbeat_at = now(), progress = %s::jsonb,"
        "  status = CASE WHEN status = 'claimed' THEN 'running' ELSE status END,"
        "  started_at = COALESCE(started_at, now())"
        " WHERE id = %s AND device_id = %s RETURNING status, cancel_requested",
        (json.dumps(progress), job_id, device_id))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job não é desta máquina")
    return {"cancel": row["cancel_requested"], "status": row["status"]}


async def gravar_eventos(job_id: str, device_id: str,
                         eventos: list[dict]) -> int:
    await job_da_maquina(job_id, device_id)
    async with db.connection() as conn:
        async with conn.transaction():
            for e in eventos:
                # idempotente: reenvio após timeout não duplica linha
                await conn.execute(
                    "INSERT INTO job_event (job_id, seq, level, message)"
                    " VALUES (%s,%s,%s,%s) ON CONFLICT (job_id, seq) DO NOTHING",
                    (job_id, e["seq"], e["level"], e["message"]))
    return len(eventos)


async def gravar_metricas(job_id: str, device_id: str,
                          metricas: list[dict]) -> int:
    await job_da_maquina(job_id, device_id)
    run = await db.fetch_one(
        "SELECT id FROM training_run WHERE job_id = %s", (job_id,))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run não encontrado")
    # NaN/inf não são JSON válido: se um entrar no banco, a tela do run quebra
    # ao ler de volta. O agente já filtra, mas a API não confia no cliente.
    finitas = [m for m in metricas if math.isfinite(m["value"])]
    async with db.connection() as conn:
        async with conn.transaction():
            for m in finitas:
                await conn.execute(
                    "INSERT INTO run_metric (run_id, epoch, key, value)"
                    " VALUES (%s,%s,%s,%s)"
                    " ON CONFLICT (run_id, epoch, key) DO UPDATE"
                    "   SET value = EXCLUDED.value, at = now()",
                    (run["id"], m["epoch"], m["key"], m["value"]))
            # melhor época pela mAP50-95: mantém training_run legível sem
            # reprocessar run_metric a cada leitura da tela.
            cur = await conn.execute(
                "SELECT epoch, value FROM run_metric"
                " WHERE run_id = %s AND key = 'mAP50-95'"
                " ORDER BY value DESC, epoch LIMIT 1", (run["id"],))
            melhor = await cur.fetchone()
            if melhor is not None:
                cur = await conn.execute(
                    "SELECT key, value FROM run_metric"
                    " WHERE run_id = %s AND epoch = %s",
                    (run["id"], melhor["epoch"]))
                resumo = {r["key"]: r["value"] for r in await cur.fetchall()}
                await conn.execute(
                    "UPDATE training_run SET metrics = %s::jsonb,"
                    " best_epoch = %s WHERE id = %s",
                    (json.dumps(resumo), melhor["epoch"], run["id"]))
    return len(finitas)


# --------------------------------------------------------------------------- #
# Pré-tagueamento: alvos e escrita das predições
# --------------------------------------------------------------------------- #

async def alvos_prelabel(job_id: str, device_id: str) -> dict:
    """Imagens que este job de pré-tag deve prever: as do dataset do escopo, e
    (se only_missing) só as que ainda não têm anotação de quem disparou."""
    job, p = await job_com_payload(job_id, device_id, jobs.Prelabel)
    autor = job["created_by"]
    if p.sample_id:
        # escopo de uma imagem só (tecla w): sem filtro de only_missing.
        rows = await db.fetch_all(
            "SELECT id, uri FROM sample WHERE id = %s AND project_id = %s",
            (p.sample_id, job["project_id"]))
    elif p.sample_ids:
        # lote específico (ex.: só a página aberta): respeita only_missing.
        rows = await db.fetch_all(
            "SELECT s.id, s.uri FROM sample s"
            " WHERE s.project_id = %(p)s AND s.id = ANY(%(ids)s::uuid[])"
            "   AND (NOT %(mo)s OR NOT EXISTS ("
            "        SELECT 1 FROM annotation a"
            "         WHERE a.sample_id = s.id AND a.author_id = %(au)s))"
            " ORDER BY s.id",
            {"p": job["project_id"], "ids": p.sample_ids,
             "mo": p.only_missing, "au": autor})
    else:
        rows = await db.fetch_all(
            """
            SELECT DISTINCT s.id, s.uri FROM sample s
              JOIN sample_source ss ON ss.sample_id = s.id
              JOIN dataset_source dsc ON dsc.data_source_id = ss.data_source_id
              JOIN dataset d ON d.id = dsc.dataset_id
             WHERE d.slug = %(ds)s AND d.project_id = %(p)s
               AND (NOT %(mo)s OR NOT EXISTS (
                    SELECT 1 FROM annotation a
                     WHERE a.sample_id = s.id AND a.author_id = %(au)s))
             ORDER BY s.id
            """, {"ds": p.dataset_slug, "p": job["project_id"],
                  "mo": p.only_missing, "au": autor})
    return {"conf": p.conf,
            "alvos": [{"sample_id": str(r["id"]), "uri": r["uri"]}
                      for r in rows]}


async def gravar_prelabels(job_id: str, device_id: str,
                           predicoes: list[dict]) -> int:
    """Grava as caixas previstas como anotação `prelabel` do autor do job.

    Nunca toca em trabalho humano: o UPDATE só acontece se a anotação existente
    também for um prelabel (re-rodar atualiza a sugestão). Predição vazia é
    ignorada — a imagem segue pendente para um humano."""
    job, p = await job_com_payload(job_id, device_id, jobs.Prelabel)
    autor = job["created_by"]
    schema_id = p.schema_id
    gravadas = 0
    async with db.connection() as conn:
        async with conn.transaction():
            for pred in predicoes:
                caixas = pred["boxes"]
                if not caixas:
                    continue
                # SELECT do sample restringe a escrita ao projeto do job: sem
                # isto um agente com um job legítimo do projeto A poderia injetar
                # prelabels em imagens do projeto B (o sample_id vem do corpo).
                cur = await conn.execute(
                    "INSERT INTO annotation (sample_id, schema_id, boxes, origin,"
                    " status, author_id, updated_by)"
                    " SELECT s.id, %s, %s::jsonb, 'prelabel', 'draft', %s, %s"
                    "   FROM sample s WHERE s.id = %s AND s.project_id = %s"
                    " ON CONFLICT (sample_id, author_id) DO UPDATE"
                    "    SET boxes = EXCLUDED.boxes, updated_at = now(),"
                    "        updated_by = EXCLUDED.updated_by"
                    "  WHERE annotation.origin = 'prelabel'"
                    " RETURNING id, (xmax = 0) AS novo",
                    (schema_id, json.dumps(caixas), autor, autor,
                     pred["sample_id"], job["project_id"]))
                row = await cur.fetchone()
                if row is not None:
                    gravadas += 1
                    # origin='prelabel' já marca que é do modelo; a ação no
                    # histórico fica no vocabulário permitido (create/update).
                    await conn.execute(
                        "INSERT INTO annotation_history (annotation_id, sample_id,"
                        " actor_id, action, boxes_after)"
                        " VALUES (%s,%s,%s,%s,%s::jsonb)",
                        (row["id"], pred["sample_id"], autor,
                         "create" if row["novo"] else "update",
                         json.dumps(caixas)))
    return gravadas


# --------------------------------------------------------------------------- #
# Registro do modelo produzido
# --------------------------------------------------------------------------- #

async def registrar_modelo(job_id: str, device_id: str, *, weights_uri: str,
                           sha256: str, tamanho: int | None,
                           metrics: dict) -> dict:
    """Cria uma versão imutável do modelo a partir do peso que o agente já subiu
    ao bucket. É INSERT único: model_version não aceita UPDATE (008)."""
    job, p = await job_com_payload(job_id, device_id, jobs.Train)
    run = await db.fetch_one(
        "SELECT id, schema_id, created_by FROM training_run WHERE job_id = %s",
        (job_id,))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run não encontrado")
    slug = p.model_slug
    async with db.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                               (f"model:{job['project_id']}:{slug}",))
            m = await db.fetch_one(
                "SELECT id FROM model WHERE project_id = %s AND slug = %s",
                (job["project_id"], slug), conn=conn)
            if m is None:
                # herda o task_type do projeto — um projeto segment/classify não
                # deve criar modelo 'detect' fixo.
                m = await db.fetch_row(
                    "INSERT INTO model (project_id, slug, name, task_type,"
                    " created_by)"
                    " SELECT %s,%s,%s, task_type, %s FROM project WHERE id = %s"
                    " RETURNING id",
                    (job["project_id"], slug, slug, run["created_by"],
                     job["project_id"]), conn=conn)
            versao = (await db.fetch_row(
                "SELECT COALESCE(max(version),0)+1 AS v FROM model_version"
                " WHERE model_id = %s", (m["id"],), conn=conn))["v"]
            mv = await db.fetch_row(
                "INSERT INTO model_version (model_id, version, run_id, schema_id,"
                " weights_uri, sha256, bytes, metrics, status)"
                " VALUES (%s,%s,%s,%s,%s,decode(%s,'hex'),%s,%s::jsonb,'ready')"
                " RETURNING id",
                (m["id"], versao, run["id"], run["schema_id"], weights_uri,
                 sha256, tamanho, json.dumps(metrics)), conn=conn)
    return {"model_slug": slug, "version": versao,
            "model_version_id": str(mv["id"])}


# --------------------------------------------------------------------------- #
# Build (artefato implantável: onnx/engine)
# --------------------------------------------------------------------------- #

async def registrar_build(job_id: str, device_id: str, *, formato: str, uri: str,
                          sha256: str, tamanho: int | None, target: dict,
                          bench: dict) -> dict:
    """Registra o artefato compilado. O device_class entra no alvo aqui — é a
    máquina que compilou que define para qual classe o engine vale."""
    job, p = await job_com_payload(job_id, device_id, jobs.Export)
    d = await _device_do_token(device_id)
    alvo = {**target, "device_class": d["device_class"]}
    row = await db.fetch_one(
        "INSERT INTO model_build (model_version_id, format, target, uri, sha256,"
        " bytes, bench, built_by_job_id, built_on_device, created_by)"
        " VALUES (%s,%s,%s::jsonb,%s,decode(%s,'hex'),%s,%s::jsonb,%s,%s,%s)"
        " ON CONFLICT (model_version_id, format, target) DO NOTHING"
        " RETURNING id",
        (p.model_version_id, formato, json.dumps(alvo), uri, sha256, tamanho,
         json.dumps(bench), job_id, device_id, job["created_by"]))
    if row is None:
        return {"ok": True, "ja_existia": True}
    return {"ok": True, "build_id": str(row["id"]), "target": alvo}


# --------------------------------------------------------------------------- #
# Avaliação (medida contra uma versão de benchmark)
# --------------------------------------------------------------------------- #

async def registrar_avaliacao(job_id: str, device_id: str, *, metrics: dict,
                              artifacts_uri: str | None) -> dict:
    """Registra a métrica do modelo naquele benchmark. Imutável — reavaliar cria
    outra linha; a linhagem (qual versão, qual benchmark) fica gravada."""
    job, p = await job_com_payload(job_id, device_id, jobs.Evaluate)
    # model_build_id distingue os artefatos (pt vs engine vs DLA) na mesma
    # versão+benchmark — é o que a comparação usa.
    row = await db.fetch_row(
        "INSERT INTO evaluation (model_version_id, model_build_id,"
        " dataset_version_id, job_id, metrics, artifacts_uri, created_by)"
        " VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s) RETURNING id",
        (p.model_version_id, p.model_build_id, p.dataset_version_id, job_id,
         json.dumps(metrics), artifacts_uri, job["created_by"]))
    return {"evaluation_id": str(row["id"]), "metrics": metrics}


# --------------------------------------------------------------------------- #
# Deployment: o device puxa o build e ativa
# --------------------------------------------------------------------------- #

async def deployments_pendentes(device_id: str) -> list[dict[str, Any]]:
    """Implantações pendentes PARA ESTA máquina. Device desativado não recebe."""
    d = await _device_do_token(device_id)
    if d["status"] == "disabled":
        return []
    return await db.fetch_all(
        "SELECT dep.id, mb.uri, mb.format, encode(mb.sha256,'hex') AS sha256,"
        "       m.slug AS model_slug, mv.version"
        "  FROM deployment dep"
        "  JOIN model_build mb ON mb.id = dep.model_build_id"
        "  JOIN model_version mv ON mv.id = mb.model_version_id"
        "  JOIN model m ON m.id = mv.model_id"
        " WHERE dep.device_id = %s AND dep.state = 'pending'"
        " ORDER BY dep.requested_at", (device_id,))


async def atualizar_deployment(dep_id: str, device_id: str, *, estado: str,
                               error: str | None) -> None:
    """O agente reporta o resultado. Ativar supersede a implantação ativa
    anterior no mesmo device — uma máquina roda um modelo por vez.

    Só sai de `pending`: o cliente do agente repete o POST em erro de rede, e um
    relato atrasado que chegasse depois do desfecho reescreveria o estado atual
    — um `active` reenviado supersedia a implantação que está valendo e
    ressuscitaria a antiga, deixando a tela mostrando um build que a máquina não
    roda. Reenviar o estado JÁ registrado é sucesso (o desfecho é o mesmo);
    qualquer outra transição é 409."""
    async with db.connection() as conn:
        async with conn.transaction():
            # Lock pelo DEVICE, não pela linha. Travar só a implantação relatada
            # (`SELECT ... FOR UPDATE` nela) não bastava: duas implantações
            # pendentes da mesma máquina ativadas ao mesmo tempo travavam linhas
            # diferentes, nenhuma enxergava o `active` não comitado da outra, e a
            # segunda batia no índice único `deployment_active_idx` — 500 para o
            # agente, com a implantação presa em 'pending' na tela.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                               (f"deployment:{device_id}",))
            # `AND device_id` no próprio WHERE é o controle de acesso: não há
            # `job_da_maquina` aqui.
            cur = await conn.execute(
                "SELECT state FROM deployment WHERE id = %s AND device_id = %s",
                (dep_id, device_id))
            dep = await cur.fetchone()
            if dep is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    "deployment não é desta máquina")
            if dep["state"] == estado:
                return
            if dep["state"] != "pending":
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"implantação já encerrada como '{dep['state']}' — relato "
                    f"atrasado, ignorado")

            if estado == "active":
                # Supersede ANTES de ativar: `deployment_active_idx` é único por
                # device sobre state='active', então a ordem inversa colidiria.
                await conn.execute(
                    "UPDATE deployment SET state = 'superseded', ended_at = now()"
                    " WHERE device_id = %s AND state = 'active' AND id <> %s",
                    (device_id, dep_id))
                await conn.execute(
                    "UPDATE deployment SET state = 'active', activated_at = now(),"
                    " error = NULL WHERE id = %s", (dep_id,))
            else:
                await conn.execute(
                    "UPDATE deployment SET state = 'failed', ended_at = now(),"
                    " error = %s WHERE id = %s", (error, dep_id))


# --------------------------------------------------------------------------- #
# Fim
# --------------------------------------------------------------------------- #

# job.status terminal → o status correspondente do training_run.
RUN_STATUS = {"succeeded": "finished", "failed": "failed", "canceled": "stopped"}
TERMINAIS = tuple(RUN_STATUS)


async def finalizar_job(job_id: str, device_id: str, *, status_final: str,
                        error: str | None, result: dict) -> None:
    run_status = RUN_STATUS[status_final]
    async with db.connection() as conn:
        async with conn.transaction():
            # O `status <> ALL` é o que impede ressurreição: o reaper e o
            # cancelamento na fila deixam o job terminal mas mantêm o
            # `device_id`, então o par (id, device_id) sozinho ainda casa quando
            # a máquina volta de uma partição de rede. Sem a guarda, o treino
            # que o usuário cancelou reaparecia como concluído com sucesso.
            cur = await conn.execute(
                "UPDATE job SET status = %s, error = %s, result = %s::jsonb,"
                "  finished_at = now() WHERE id = %s AND device_id = %s"
                "  AND status <> ALL(%s) RETURNING id",
                (status_final, error, json.dumps(result), job_id, device_id,
                 list(TERMINAIS)))
            if await cur.fetchone() is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    "job não é desta máquina")
            # duração = tempo de EXECUÇÃO, não a espera na fila: job.started_at
            # é setado no 1º heartbeat (início real); training_run.started_at
            # nasce no enfileiramento. Usa o do job, caindo no do run se ausente.
            await conn.execute(
                "UPDATE training_run r SET status = %s, finished_at = now(),"
                "  duration_s = EXTRACT(EPOCH FROM (now() - COALESCE("
                "     (SELECT started_at FROM job WHERE id = %s), r.started_at)))::int"
                " WHERE r.job_id = %s", (run_status, job_id, job_id))
            await conn.execute(
                "UPDATE device SET status = 'idle' WHERE id = %s"
                "  AND status = 'busy'", (device_id,))
