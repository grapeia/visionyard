"""Aba Dados: as imagens do projeto e a anotação de cada uma.

Anotação é por (imagem, autor). Salvar mexe só na sua: o trabalho de outro
rotulador não é tocado, e é isso que permite medir concordância depois.

As imagens são buscadas pelo navegador **direto do GCS**, por URL assinada — a
API emite a URL e não trafega bytes. Medido: a galeria sai de 28 s para 1,7 s
por página. O gargalo nunca foi o tamanho da imagem, era passar por um
intermediário.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from starlette.concurrency import run_in_threadpool

from .. import db, storage

POR_PAGINA = 60
MINIATURA_MIN = 15   # validade da URL assinada, em minutos

# `minha` distingue "anotada por alguém" de "anotada por mim": o rotulador
# precisa achar o que falta PARA ELE, não o que falta no geral.
CONDICAO = {"todas": "", "pendentes": " AND a.id IS NULL",
            "anotadas": " AND a.id IS NOT NULL"}

MINHA = (" LEFT JOIN annotation a"
         "   ON a.sample_id = s.id AND a.author_id = %(u)s")

# As imagens do dataset = as das origens que ele referencia.
ESCOPO = """AND EXISTS (
    SELECT 1 FROM sample_source ss
      JOIN dataset_source dsc ON dsc.data_source_id = ss.data_source_id
      JOIN dataset d ON d.id = dsc.dataset_id
     WHERE ss.sample_id = s.id AND d.slug = %(ds)s
       AND d.project_id = %(p)s)"""


def _de_onde(juncao: str, escopo: str, cond: str) -> str:
    """O `FROM ... WHERE` da grade, escrito uma vez só.

    Estava duplicado entre a página e a contagem, e foi a duplicação que
    produziu o bug do `da7e21f` — o escopo entrou num lado e não no outro.
    Montar o rabo aqui torna a divergência impossível em vez de remendada.
    """
    return f"FROM sample s{juncao} WHERE s.project_id = %(p)s {escopo} {cond}"


async def listar(project_id: str, user_id: str, *, pagina: int = 0,
                 filtro: str = "todas", dataset: str | None = None) -> dict:
    """Página de imagens, com URL assinada para o navegador buscar direto.

    `dataset` é escopo, não posse: a anotação vive no sample, não no dataset,
    porque a mesma imagem pode estar em vários. O escopo só restringe QUAIS
    imagens você vê — as das origens daquele dataset — para focar o esforço de
    rotulagem. A caixa que você desenha aqui vale para todo dataset que inclua
    a imagem.
    """
    cond = CONDICAO[filtro]
    escopo = ESCOPO if dataset else ""
    params = {"p": project_id, "u": user_id, "ds": dataset,
              "lim": POR_PAGINA, "off": pagina * POR_PAGINA}

    # O total sai da MESMA consulta que a grade, por janela. Contar à parte
    # pegava outra conexão e, sob READ COMMITTED, outro snapshot: uma anotação
    # gravada entre as duas idas reabria o descompasso entre o rodapé e o que
    # a grade mostra. Custa ordenar o conjunto filtrado inteiro em vez de parar
    # nos 60 da página — trabalho que a contagem à parte já fazia de qualquer
    # forma, agora feito uma vez.
    async with db.connection() as conn:
        linhas = await db.fetch_all(
            f"""SELECT s.id, s.uri, s.width, s.height, s.metadata,
                       a.id AS annotation_id, a.status, a.origin,
                       jsonb_array_length(COALESCE(a.boxes,'[]'::jsonb))
                           AS n_caixas,
                       count(*) OVER () AS total
                  {_de_onde(MINHA, escopo, cond)}
                 ORDER BY s.imported_at, s.id
                 LIMIT %(lim)s OFFSET %(off)s""", params, conn=conn)

        if linhas:
            total = linhas[0]["total"]
        else:
            # Página vazia não tem de onde tirar a janela, e "0 imagens" sobre
            # uma grade vazia esconderia que basta voltar uma página. Aqui a
            # junção com `annotation` só entra se o filtro falar dela: com
            # `filtro=todas`, que é o padrão da aba, ela era trabalho jogado
            # fora em toda carga.
            total = (await db.fetch_row(
                f"SELECT count(*) AS n"
                f" {_de_onde(MINHA if cond else '', escopo, cond)}",
                params, conn=conn))["n"]

    # Assinar fala com o GCS, e é por isso que acontece com a conexão já
    # devolvida: uma ida à rede com transação aberta deixa a sessão
    # `idle in transaction` e prende uma das dez vagas do pool esperando o
    # Google responder.
    assinadas = await run_in_threadpool(
        storage.assinar, [r["uri"] for r in linhas], MINIATURA_MIN)
    itens = [{**r, "url": assinadas.get(r["uri"])} for r in linhas]
    for it in itens:
        del it["total"]          # coluna de janela: é da página, não do item
    return {"pagina": pagina, "por_pagina": POR_PAGINA, "total": total,
            "assina": bool(assinadas), "itens": itens}


async def detalhe(project_slug: str, sample_id: UUID,
                  user_id: str) -> dict[str, Any]:
    async with db.connection() as conn:
        s = await db.fetch_one(
            "SELECT s.id, s.uri, s.width, s.height, s.metadata"
            "  FROM sample s JOIN project p ON p.id = s.project_id"
            " WHERE p.slug = %s AND s.id = %s", (project_slug, sample_id),
            conn=conn)
        if s is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "imagem não encontrada")
        minha = await db.fetch_one(
            "SELECT id, boxes, status, origin, updated_at FROM annotation"
            " WHERE sample_id = %s AND author_id = %s", (sample_id, user_id),
            conn=conn)
        # As dos outros aparecem só como contagem: ver a caixa alheia antes de
        # desenhar a sua enviesa a anotação e destrói a medida de concordância.
        outros = await db.fetch_row(
            "SELECT count(*) AS n FROM annotation"
            " WHERE sample_id = %s AND author_id <> %s", (sample_id, user_id),
            conn=conn)
    url = (await run_in_threadpool(
        storage.assinar, [s["uri"]], MINIATURA_MIN)).get(s["uri"])
    return {**s, "url": url, "minha_anotacao": minha,
            "outros_anotaram": outros["n"]}


async def salvar_anotacao(project_slug: str, sample_id: UUID, user_id: str,
                          caixas: list[dict[str, Any]],
                          estado: str) -> dict[str, Any]:
    """Grava a anotação de UM autor. A de outro rotulador não é tocada."""
    async with db.connection() as conn, conn.transaction():
        ctx = await db.fetch_one(
            "SELECT s.id, p.id AS project_id,"
            "  (SELECT id FROM label_schema WHERE project_id = p.id"
            "    ORDER BY version DESC LIMIT 1) AS schema_id,"
            "  (SELECT classes FROM label_schema WHERE project_id = p.id"
            "    ORDER BY version DESC LIMIT 1) AS classes"
            "  FROM sample s JOIN project p ON p.id = s.project_id"
            " WHERE p.slug = %s AND s.id = %s", (project_slug, sample_id),
            conn=conn)
        if ctx is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "imagem não encontrada")

        n_classes = len(ctx["classes"])
        fora = [b["cls"] for b in caixas if b["cls"] >= n_classes]
        if fora:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"classe {fora[0]} não existe neste projeto "
                f"({n_classes} classes)")

        # Lido só para `boxes_before`. Nem a origem nem a ação saem daqui:
        # a origem é decidida no `DO UPDATE` e a ação vem do `xmax`, os dois
        # sobre a linha que o próprio comando trava. `boxes_before` fica sem
        # essa garantia de propósito — não há como o `RETURNING` devolver o
        # valor ANTIGO de uma coluna que ele acabou de sobrescrever —, então
        # numa corrida com o pré-tag ele pode vir nulo num update. É trilha,
        # não invariante: perder a caixa anterior é ruim, dizer "create" onde
        # houve sobrescrita é mentira.
        antes = await db.fetch_one(
            "SELECT boxes FROM annotation"
            " WHERE sample_id = %s AND author_id = %s", (sample_id, user_id),
            conn=conn)

        # Corrigir uma pré-rotulagem é 'correction', não 'manual': a diferença
        # é o que permite medir depois quanto o humano mudou do que o modelo
        # sugeriu. Se essa taxa cair muito, o ciclo virou autoconfirmação — o
        # modelo treinando na própria saída. 'correction' está na tupla porque
        # a procedência é do trabalho, não do último clique: sem ela o segundo
        # save da mesma imagem a rebaixava para 'manual' e a taxa zerava justo
        # onde o humano mais mexeu.
        #
        # O `CASE` lê `annotation.origin` — a linha existente, dentro do mesmo
        # comando. Decidir isso em Python, a partir de uma leitura anterior,
        # deixava a janela em que um pré-tag chega entre a leitura e a escrita:
        # a taxa contaria 'manual' onde houve correção, e é justamente ela que
        # o módulo existe para medir.
        nova = await db.fetch_row(
            "INSERT INTO annotation (sample_id, schema_id, boxes, origin,"
            " status, author_id, updated_by)"
            " VALUES (%s,%s,%s::jsonb,'manual',%s,%s,%s)"
            " ON CONFLICT (sample_id, author_id) DO UPDATE"
            "    SET boxes = EXCLUDED.boxes, status = EXCLUDED.status,"
            "        origin = CASE WHEN annotation.origin"
            "                        IN ('prelabel','import','correction')"
            "                      THEN 'correction' ELSE 'manual' END,"
            "        updated_at = now(), updated_by = EXCLUDED.updated_by"
            " RETURNING id, status, origin, (xmax = 0) AS novo",
            (sample_id, ctx["schema_id"], json.dumps(caixas), estado, user_id,
             user_id), conn=conn)
        await db.execute(
            "INSERT INTO annotation_history (annotation_id, sample_id,"
            " actor_id, action, boxes_before, boxes_after)"
            " VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb)",
            (nova["id"], sample_id, user_id,
             "create" if nova["novo"] else "update",
             json.dumps(antes["boxes"]) if antes else None,
             json.dumps(caixas)), conn=conn)
    return {"id": nova["id"], "status": nova["status"],
            "origin": nova["origin"], "caixas": len(caixas)}
