"""Projetos: criação, listagem e ciclo de vida.

Criar um projeto cria também o LabelSchema v1 e torna quem criou o `owner` —
as três coisas na mesma transação, porque um projeto sem vocabulário não aceita
anotação e um projeto sem owner não aceita gestão de membros.
"""
from __future__ import annotations

import json
import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, Field, field_validator

from .. import auth, db
from ..auth import Principal
from ..deps import CurrentUser, audit, project_role, require_role, require_superuser

router = APIRouter(tags=["projects"])

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class ProjectCreateIn(BaseModel):
    slug: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    task_type: Literal["detect", "segment", "classify"] = "detect"
    description: str | None = None
    # As classes do LabelSchema v1. Ordem importa: é o índice do formato YOLO.
    classes: list[str] = Field(min_length=1)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not SLUG_RE.match(v):
            raise ValueError("slug deve ser minúsculo, alfanumérico, "
                             "separado por hífen (ex: linha-de-inspecao)")
        return v

    @field_validator("classes")
    @classmethod
    def _classes(cls, v: list[str]) -> list[str]:
        out = [c.strip() for c in v if c.strip()]
        if not out:
            raise ValueError("informe ao menos uma classe")
        if len(set(out)) != len(out):
            raise ValueError("classes duplicadas")
        return out


class ProjectPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class SchemaAppendIn(BaseModel):
    # Só os nomes a ACRESCENTAR no fim — não a lista inteira. Assim a operação é
    # sempre append por construção: não há como renomear, remover ou reordenar
    # por aqui, que são as mudanças que quebram a compatibilidade.
    add: list[str] = Field(min_length=1)
    change_note: str | None = Field(default=None, max_length=200)

    @field_validator("add")
    @classmethod
    def _add(cls, v: list[str]) -> list[str]:
        out = [c.strip() for c in v if c.strip()]
        if not out:
            raise ValueError("informe ao menos uma classe")
        if len(set(out)) != len(out):
            raise ValueError("classes duplicadas na lista")
        return out


@router.get("/projects")
async def list_projects(user: CurrentUser):
    """Projetos que eu enxergo, com meu papel em cada um."""
    if user.is_superuser:
        return await db.fetch_all(
            "SELECT p.slug, p.name, p.task_type, p.description, p.created_at,"
            "       'owner' AS role"
            "  FROM project p WHERE p.archived_at IS NULL ORDER BY p.name")
    return await db.fetch_all(
        "SELECT p.slug, p.name, p.task_type, p.description, p.created_at,"
        "       m.role"
        "  FROM project p JOIN project_member m ON m.project_id = p.id"
        " WHERE m.user_id = %s AND p.archived_at IS NULL ORDER BY p.name",
        (user.id,))


@router.post("/projects", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_superuser)])
async def create_project(body: ProjectCreateIn, user: CurrentUser,
                         request: Request):
    """Criar projeto exige superusuário.

    Sem isso, qualquer usuário autenticado criava um projeto, virava `owner`
    dele e com isso ganhava `engineer` — que é justamente o papel exigido para
    navegar buckets e importar. Todo o controle por papel era auto-concedível
    em uma requisição.
    """
    exists = await db.fetch_one("SELECT id FROM project WHERE slug = %s",
                                (body.slug,))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"já existe projeto com slug '{body.slug}'")

    classes = [{"id": i, "name": c} for i, c in enumerate(body.classes)]

    async with db.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "INSERT INTO project (slug, name, task_type, description,"
                " created_by) VALUES (%s,%s,%s,%s,%s)"
                " RETURNING id, slug, name, task_type, description, created_at",
                (body.slug, body.name, body.task_type, body.description,
                 user.id))
            proj = await cur.fetchone()
            await conn.execute(
                "INSERT INTO project_member (project_id, user_id, role,"
                " granted_by) VALUES (%s,%s,'owner',%s)",
                (proj["id"], user.id, user.id))
            await conn.execute(
                "INSERT INTO label_schema (project_id, version, classes,"
                " change_note, created_by) VALUES (%s,1,%s::jsonb,%s,%s)",
                (proj["id"], json.dumps(classes), "schema inicial", user.id))
            await audit(user, "project.create", "project", str(proj["id"]),
                        project_id=str(proj["id"]),
                        after={"slug": body.slug, "classes": body.classes},
                        ip=auth.client_ip(request), conn=conn)
    return {**proj, "role": "owner", "classes": body.classes}


@router.get("/projects/{project_slug}",
            dependencies=[Depends(require_role("viewer"))])
async def get_project(project_slug: Annotated[str, Path()], user: CurrentUser):
    proj = await db.fetch_one(
        "SELECT id, slug, name, task_type, description, created_at,"
        "       archived_at FROM project WHERE slug = %s", (project_slug,))
    schema = await db.fetch_one(
        "SELECT version, classes FROM label_schema WHERE project_id = %s"
        " ORDER BY version DESC LIMIT 1", (proj["id"],))
    # Histórico enxuto: a aba Projeto mostra a linha do tempo do vocabulário.
    schemas = await db.fetch_all(
        "SELECT version, back_compatible, change_note, created_at,"
        "       jsonb_array_length(classes) AS n_classes"
        "  FROM label_schema WHERE project_id = %s ORDER BY version DESC",
        (proj["id"],))
    counts = await db.fetch_one(
        "SELECT (SELECT count(*) FROM sample WHERE project_id = %(p)s) AS samples,"
        "       (SELECT count(*) FROM annotation a JOIN sample s"
        "          ON s.id = a.sample_id WHERE s.project_id = %(p)s) AS annotations,"
        "       (SELECT count(*) FROM dataset WHERE project_id = %(p)s) AS datasets,"
        "       (SELECT count(*) FROM model WHERE project_id = %(p)s) AS models",
        {"p": proj["id"]})
    return {**proj, "role": await project_role(project_slug, user),
            "schema": schema, "schemas": schemas, "counts": counts}


@router.patch("/projects/{project_slug}")
async def patch_project(body: ProjectPatchIn,
                        project_slug: Annotated[str, Path()], request: Request,
                        actor: Annotated[Principal, Depends(require_role("admin"))]):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "nada a alterar")
    before = await db.fetch_one(
        "SELECT id, name, description FROM project WHERE slug = %s",
        (project_slug,))
    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    async with db.connection() as conn:
        async with conn.transaction():
            row = await db.fetch_one(
                f"UPDATE project SET {sets} WHERE slug = %(slug)s"
                " RETURNING id, slug, name, task_type, description",
                {**fields, "slug": project_slug}, conn=conn)
            await audit(actor, "project.update", "project", str(before["id"]),
                        project_id=str(before["id"]),
                        before={k: before[k] for k in fields}, after=fields,
                        ip=auth.client_ip(request), conn=conn)
    return row


@router.post("/projects/{project_slug}/schema",
             status_code=status.HTTP_201_CREATED)
async def append_classes(
        body: SchemaAppendIn, project_slug: Annotated[str, Path()],
        request: Request,
        actor: Annotated[Principal, Depends(require_role("engineer"))]):
    """Acrescenta classe(s) no fim → nova versão de schema, back-compatible.

    O schema é imutável (não há UPDATE): toda mudança é uma versão nova. Como só
    acrescenta no fim, os índices antigos não mudam e nada do que já foi rotulado
    é invalidado — `schemas_compativeis` continua contando as anotações das
    versões anteriores. Renomear/remover/reordenar não passa por aqui: quebraria
    a compatibilidade e mandaria anotações para needs_review, e é operação para
    um caminho próprio, com as devidas travas.
    """
    proj = await db.fetch_one("SELECT id FROM project WHERE slug = %s",
                              (project_slug,))
    if proj is None:      # superuser passa por require_role sem checar existência
        raise HTTPException(status.HTTP_404_NOT_FOUND, "projeto não encontrado")
    async with db.connection() as conn:
        async with conn.transaction():
            # Serializa por projeto: dois appends simultâneos calculariam a mesma
            # versão e colidiriam no UNIQUE(project_id, version).
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                               (f"schema:{proj['id']}",))
            cur = await conn.execute(
                "SELECT version, classes FROM label_schema WHERE project_id = %s"
                " ORDER BY version DESC LIMIT 1", (proj["id"],))
            atual = await cur.fetchone()
            nomes = [c["name"] for c in atual["classes"]]

            ja = [n for n in body.add if n in nomes]
            if ja:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"classe '{ja[0]}' já existe neste projeto")

            novas = atual["classes"] + [
                {"id": len(nomes) + i, "name": n}
                for i, n in enumerate(body.add)]
            nota = body.change_note or f"adicionada(s): {', '.join(body.add)}"
            cur = await conn.execute(
                "INSERT INTO label_schema (project_id, version, classes,"
                " back_compatible, change_note, created_by)"
                " VALUES (%s,%s,%s::jsonb,true,%s,%s) RETURNING id, version",
                (proj["id"], atual["version"] + 1, json.dumps(novas), nota,
                 actor.id))
            nova = await cur.fetchone()

            await audit(actor, "schema.append", "label_schema",
                        str(nova["id"]), project_id=str(proj["id"]),
                        before={"classes": nomes},
                        after={"classes": [c["name"] for c in novas]},
                        ip=auth.client_ip(request), conn=conn)
    return {"version": nova["version"],
            "classes": [c["name"] for c in novas]}


@router.post("/projects/{project_slug}/archive",
             status_code=status.HTTP_204_NO_CONTENT)
async def archive_project(project_slug: Annotated[str, Path()],
                          request: Request,
                          actor: Annotated[Principal, Depends(require_role("owner"))]):
    """Arquiva (some das listagens). Não deleta: os dados seguem referenciados
    por datasets, runs e modelos, e apagá-los quebraria a linhagem."""
    async with db.connection() as conn:
        async with conn.transaction():
            row = await db.fetch_one(
                "UPDATE project SET archived_at = now()"
                " WHERE slug = %s AND archived_at IS NULL RETURNING id",
                (project_slug,), conn=conn)
            if row is None:
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    "projeto já arquivado")
            await audit(actor, "project.archive", "project", str(row["id"]),
                        project_id=str(row["id"]),
                        ip=auth.client_ip(request), conn=conn)
