"""Projetos: criação, ciclo de vida e o vocabulário (LabelSchema).

Criar um projeto cria também o LabelSchema v1 e torna quem criou o `owner` —
as três coisas na mesma transação, porque um projeto sem vocabulário não aceita
anotação e um projeto sem owner não aceita gestão de membros.

O schema é **imutável**: não há UPDATE em `label_schema`, toda mudança é uma
versão nova. E a única mudança que passa por aqui é acrescentar no fim, que é a
única que não invalida o que já foi rotulado.

Segue a forma do item 15 do PLANO, igual a `services/datasets.py`: toda função
que fala com o banco recebe `conn`, quem abre a conexão é a função pública, e o
`conn.transaction()` vai no MESMO `async with` da conexão — depois de uma
leitura ele viraria um `SAVEPOINT`, com o commit acontecendo lá longe, quando o
pool devolvesse a conexão.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, status

from .. import db
from ..audit import audit
from ..auth import Principal
from ..deps import project_role


async def projeto_ou_404(conn: db.Conn, project_slug: str,
                          colunas: str = "id") -> dict[str, Any]:
    """O projeto da rota, ou 404.

    `require_role` **não** garante que o projeto existe: superusuário recebe
    'owner' sem consultar nada, então um slug inexistente atravessa a
    dependência e viraria 500 no primeiro `proj["id"]`.

    `colunas` é SQL literal escrito aqui — nunca valor de requisição.

    Pública porque `services/users.py` também precisa dela: é a mesma pergunta,
    e ter duas cópias faria a segunda envelhecer sozinha. É a versão com `conn`
    da `deps.projeto_ou_404`, que alcança o pool.
    """
    row = await db.fetch_one(
        f"SELECT {colunas} FROM project WHERE slug = %s", (project_slug,),
        conn=conn)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "projeto não encontrado")
    return row


async def listar(user: Principal) -> list[dict[str, Any]]:
    """Projetos que este usuário enxerga, com o papel dele em cada um."""
    async with db.connection() as conn:
        if user.is_superuser:
            return await db.fetch_all(
                "SELECT p.slug, p.name, p.task_type, p.description,"
                "       p.created_at, 'owner' AS role"
                "  FROM project p WHERE p.archived_at IS NULL ORDER BY p.name",
                conn=conn)
        return await db.fetch_all(
            "SELECT p.slug, p.name, p.task_type, p.description, p.created_at,"
            "       m.role"
            "  FROM project p JOIN project_member m ON m.project_id = p.id"
            " WHERE m.user_id = %s AND p.archived_at IS NULL ORDER BY p.name",
            (user.id,), conn=conn)


def _vocabulario(atuais: list[dict[str, Any]], novas: list[dict[str, Any]],
                 task_type: str) -> list[dict[str, Any]]:
    """As classes de `atuais` com `novas` acrescentadas no fim, já com id.

    `novas` vem como `{"name", "inside"}`, com `inside` sendo o NOME da classe
    principal — é o que a tela conhece. Gravado, vira o `id` dela: o nome
    poderia um dia ganhar um caminho de renomear, o índice YOLO não muda.

    "Dentro de" só existe em segmentação, só aponta para classe que já existe
    (antes desta, na mesma lista ou numa versão anterior) e só para principal:
    um nível, por ora. Recorte dentro de recorte pediria ao anotador recortar
    por um contorno que também é recortado, e ninguém pediu isso ainda.
    """
    if task_type != "segment" and any(c["inside"] for c in novas):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "'Dentro de' só existe em projeto de segmentação")
    classes = list(atuais)
    por_nome = {c["name"]: c for c in classes}
    for c in novas:
        nova: dict[str, Any] = {"id": len(classes), "name": c["name"]}
        if c["inside"]:
            pai = por_nome.get(c["inside"])
            if pai is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"classe '{c['inside']}' não existe para '{c['name']}'"
                    " ficar dentro dela")
            if pai.get("inside") is not None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"'{pai['name']}' já é recorte; 'Dentro de' aponta só"
                    " para classe principal")
            nova["inside"] = pai["id"]
        classes.append(nova)
        por_nome[nova["name"]] = nova
    return classes


async def criar(user: Principal, *, slug: str, name: str, task_type: str,
                description: str | None, classes: list[dict[str, Any]],
                ip: str | None) -> dict[str, Any]:
    """Projeto + owner + schema v1, na mesma transação."""
    classes_json = _vocabulario([], classes, task_type)
    nomes = [c["name"] for c in classes_json]
    async with db.connection() as conn, conn.transaction():
        existe = await db.fetch_one(
            "SELECT id FROM project WHERE slug = %s", (slug,), conn=conn)
        if existe:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"já existe projeto com slug '{slug}'")
        proj = await db.fetch_row(
            "INSERT INTO project (slug, name, task_type, description,"
            " created_by) VALUES (%s,%s,%s,%s,%s)"
            " RETURNING id, slug, name, task_type, description, created_at",
            (slug, name, task_type, description, user.id), conn=conn)
        await db.execute(
            "INSERT INTO project_member (project_id, user_id, role,"
            " granted_by) VALUES (%s,%s,'owner',%s)",
            (proj["id"], user.id, user.id), conn=conn)
        await db.execute(
            "INSERT INTO label_schema (project_id, version, classes,"
            " change_note, created_by) VALUES (%s,1,%s::jsonb,%s,%s)",
            (proj["id"], json.dumps(classes_json), "schema inicial", user.id),
            conn=conn)
        await audit(user, "project.create", "project", str(proj["id"]),
                    project_id=str(proj["id"]),
                    after={"slug": slug, "classes": nomes},
                    ip=ip, conn=conn)
    return {**proj, "role": "owner", "classes": nomes}


async def detalhe(project_slug: str, user: Principal) -> dict[str, Any]:
    async with db.connection() as conn:
        proj = await projeto_ou_404(
            conn, project_slug,
            "id, slug, name, task_type, description, created_at, archived_at")
        schema = await db.fetch_one(
            "SELECT version, classes FROM label_schema WHERE project_id = %s"
            " ORDER BY version DESC LIMIT 1", (proj["id"],), conn=conn)
        # Histórico enxuto: a aba Projeto mostra a linha do tempo do
        # vocabulário.
        schemas = await db.fetch_all(
            "SELECT version, back_compatible, change_note, created_at,"
            "       jsonb_array_length(classes) AS n_classes"
            "  FROM label_schema WHERE project_id = %s ORDER BY version DESC",
            (proj["id"],), conn=conn)
        counts = await db.fetch_row(
            "SELECT (SELECT count(*) FROM sample"
            "          WHERE project_id = %(p)s) AS samples,"
            "       (SELECT count(*) FROM annotation a JOIN sample s"
            "          ON s.id = a.sample_id"
            "         WHERE s.project_id = %(p)s) AS annotations,"
            "       (SELECT count(*) FROM dataset"
            "          WHERE project_id = %(p)s) AS datasets,"
            "       (SELECT count(*) FROM model"
            "          WHERE project_id = %(p)s) AS models",
            {"p": proj["id"]}, conn=conn)
        papel = await project_role(project_slug, user, conn=conn)
    return {**proj, "role": papel, "schema": schema, "schemas": schemas,
            "counts": counts}


async def alterar(project_slug: str, actor: Principal, *,
                  campos: dict[str, Any], ip: str | None) -> dict[str, Any]:
    """Renomeia ou redescreve. `campos` já vem só com o que foi enviado."""
    if not campos:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "nada a alterar")
    sets = ", ".join(f"{k} = %({k})s" for k in campos)
    async with db.connection() as conn, conn.transaction():
        antes = await projeto_ou_404(conn, project_slug,
                                      "id, name, description")
        # `fetch_row` e não `fetch_one`: o 404 já saiu acima, e agora a leitura
        # e o UPDATE estão na mesma transação — a linha existe, e vir vazia
        # aqui seria bug de programação, não slug errado.
        row = await db.fetch_row(
            f"UPDATE project SET {sets} WHERE slug = %(slug)s"
            " RETURNING id, slug, name, task_type, description",
            {**campos, "slug": project_slug}, conn=conn)
        await audit(actor, "project.update", "project", str(antes["id"]),
                    project_id=str(antes["id"]),
                    before={k: antes[k] for k in campos}, after=campos,
                    ip=ip, conn=conn)
    return row


async def acrescentar_classes(project_slug: str, actor: Principal, *,
                              add: list[dict[str, Any]],
                              change_note: str | None,
                              ip: str | None) -> dict[str, Any]:
    """Acrescenta classe(s) no fim → nova versão de schema, back-compatible.

    Como só acrescenta no fim, os índices antigos não mudam e nada do que já foi
    rotulado é invalidado — `schemas_compativeis` continua contando as anotações
    das versões anteriores. Renomear/remover/reordenar não passa por aqui:
    quebraria a compatibilidade e mandaria anotações para needs_review, e é
    operação para um caminho próprio, com as devidas travas.
    """
    async with db.connection() as conn, conn.transaction():
        proj = await projeto_ou_404(conn, project_slug, "id, task_type")
        # Serializa por projeto: dois appends simultâneos calculariam a mesma
        # versão e colidiriam no UNIQUE(project_id, version).
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                           (f"schema:{proj['id']}",))
        # todo projeto nasce com o schema v1 (`criar`, mesma transação):
        # existe projeto sem imagem, nunca sem schema
        atual = await db.fetch_row(
            "SELECT version, classes FROM label_schema WHERE project_id = %s"
            " ORDER BY version DESC LIMIT 1", (proj["id"],), conn=conn)
        nomes = [c["name"] for c in atual["classes"]]

        ja = [c["name"] for c in add if c["name"] in nomes]
        if ja:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"classe '{ja[0]}' já existe neste projeto")

        novas = _vocabulario(atual["classes"], add, proj["task_type"])
        nota = change_note or (
            f"adicionada(s): {', '.join(c['name'] for c in add)}")
        nova = await db.fetch_row(
            "INSERT INTO label_schema (project_id, version, classes,"
            " back_compatible, change_note, created_by)"
            " VALUES (%s,%s,%s::jsonb,true,%s,%s) RETURNING id, version",
            (proj["id"], atual["version"] + 1, json.dumps(novas), nota,
             actor.id), conn=conn)

        await audit(actor, "schema.append", "label_schema", str(nova["id"]),
                    project_id=str(proj["id"]), before={"classes": nomes},
                    after={"classes": [c["name"] for c in novas]},
                    ip=ip, conn=conn)
    return {"version": nova["version"], "classes": [c["name"] for c in novas]}


async def arquivar(project_slug: str, actor: Principal, *,
                   ip: str | None) -> None:
    """Some das listagens. Não deleta: os dados seguem referenciados por
    datasets, runs e modelos, e apagá-los quebraria a linhagem."""
    async with db.connection() as conn, conn.transaction():
        # O 404 vem antes porque o UPDATE não distingue "não existe" de "já
        # arquivado": sem isto, um slug digitado errado respondia 409 "projeto
        # já arquivado" e um script que trata 409 como "já feito" dava por
        # concluído um projeto que nunca existiu.
        await projeto_ou_404(conn, project_slug)
        row = await db.fetch_one(
            "UPDATE project SET archived_at = now()"
            " WHERE slug = %s AND archived_at IS NULL RETURNING id",
            (project_slug,), conn=conn)
        if row is None:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "projeto já arquivado")
        await audit(actor, "project.archive", "project", str(row["id"]),
                    project_id=str(row["id"]), ip=ip, conn=conn)


# ---------------------------------------------------------------- gabarito --

async def gabaritos(project_slug: str) -> list[dict[str, Any]]:
    """Os gabaritos de corte do projeto, um por classe principal."""
    async with db.connection() as conn:
        proj = await projeto_ou_404(conn, project_slug)
        return await db.fetch_all(
            "SELECT class_index, lines, seeds, shapes, updated_at"
            "  FROM segment_template"
            " WHERE project_id = %s ORDER BY class_index", (proj["id"],),
            conn=conn)


async def gravar_gabarito(project_slug: str, actor: Principal, *,
                          class_index: int, lines: list[Any],
                          seeds: list[dict[str, Any]],
                          shapes: list[dict[str, Any]],
                          ip: str | None) -> dict[str, Any]:
    """Grava (substituindo) o gabarito de cortes de uma classe principal.

    Confere contra o schema atual o que o anotador não tem como garantir: que
    a classe é principal e que cada semente e cada laço é de um recorte DELA.
    Classe de fora daria ao pedaço da banda uma classe que não está "dentro
    de" banda, e o anotador a gravaria assim.
    """
    async with db.connection() as conn, conn.transaction():
        proj = await projeto_ou_404(conn, project_slug, "id, task_type")
        if proj["task_type"] != "segment":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "gabarito de cortes só existe em projeto de "
                                "segmentação")
        schema = await db.fetch_row(
            "SELECT classes FROM label_schema WHERE project_id = %s"
            " ORDER BY version DESC LIMIT 1", (proj["id"],), conn=conn)
        classes = schema["classes"]
        if class_index >= len(classes):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"classe {class_index} não existe")
        principal = classes[class_index]
        if principal.get("inside") is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"'{principal['name']}' é recorte: gabarito é "
                                "de classe principal")
        filhas = {i for i, c in enumerate(classes)
                  if c.get("inside") == principal["id"]}
        fora = [x["cls"] for x in [*seeds, *shapes] if x["cls"] not in filhas]
        if fora:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"a classe {fora[0]} não está \"dentro de\" "
                                f"'{principal['name']}'")
        row = await db.fetch_row(
            "INSERT INTO segment_template"
            " (project_id, class_index, lines, seeds, shapes, updated_by)"
            " VALUES (%s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)"
            " ON CONFLICT (project_id, class_index) DO UPDATE SET"
            "   lines = EXCLUDED.lines, seeds = EXCLUDED.seeds,"
            "   shapes = EXCLUDED.shapes,"
            "   updated_by = EXCLUDED.updated_by, updated_at = now()"
            " RETURNING class_index, lines, seeds, shapes, updated_at",
            (proj["id"], class_index, json.dumps(lines), json.dumps(seeds),
             json.dumps(shapes), actor.id), conn=conn)
        # o alvo é o projeto: a chave do gabarito é composta, e `target_id`
        # é uuid; a classe vai no `after`
        await audit(actor, "template.save", "project", str(proj["id"]),
                    project_id=str(proj["id"]),
                    after={"class": principal["name"], "lines": len(lines),
                           "seeds": len(seeds), "shapes": len(shapes)},
                    ip=ip, conn=conn)
    return row
