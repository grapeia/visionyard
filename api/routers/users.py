"""Gestão de usuários e de papéis por projeto."""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, EmailStr, Field

from .. import auth, db
from ..auth import Principal
from ..config import settings
from ..deps import (
    ROLE_RANK,
    CurrentUser,
    audit,
    project_role,
    require_role,
    require_superuser,
)
from ..security import hash_password

router = APIRouter(tags=["users"])

Role = Literal["owner", "admin", "engineer", "annotator", "viewer"]


class UserCreateIn(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1)
    # Sem senha: usuário nasce 'invited' e a define no primeiro acesso.
    password: str | None = Field(default=None,
                                 min_length=settings.password_min_length)
    is_superuser: bool = False


class MemberIn(BaseModel):
    email: EmailStr
    role: Role


@router.get("/users", dependencies=[Depends(require_superuser)])
async def list_users():
    return await db.fetch_all(
        "SELECT id, email, display_name, status, is_superuser, created_at,"
        "       last_login_at FROM app_user ORDER BY display_name")


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(body: UserCreateIn, request: Request,
                      admin: Annotated[Principal, Depends(require_superuser)]):
    exists = await db.fetch_one("SELECT id FROM app_user WHERE lower(email)=%s",
                                (body.email.lower(),))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "email já cadastrado")

    async with db.connection() as conn:
        async with conn.transaction():
            row = await db.fetch_one(
                "INSERT INTO app_user (email, display_name, password_hash,"
                " status, is_superuser, created_by) VALUES (%s,%s,%s,%s,%s,%s)"
                " RETURNING id, email, display_name, status, is_superuser",
                (body.email.lower(), body.display_name,
                 hash_password(body.password) if body.password else None,
                 "active" if body.password else "invited",
                 body.is_superuser, admin.id), conn=conn)
            await audit(admin, "user.create", "app_user", str(row["id"]),
                        after={"email": row["email"],
                               "superuser": body.is_superuser},
                        ip=auth.client_ip(request), conn=conn)
    return row


@router.post("/users/{user_id}/invite", status_code=status.HTTP_201_CREATED)
async def invite_user(user_id: str, request: Request,
                      admin: Annotated[Principal, Depends(require_superuser)],
                      purpose: Literal["invite", "password_reset"] = "invite"):
    """Emite um convite de uso único e devolve o token.

    O token sai na resposta em vez de ir por email porque ainda não há serviço
    de email configurado — quem administra repassa o link pelo canal que usar.
    Emitir um novo invalida o anterior.
    """
    target = await db.fetch_one(
        "SELECT id, email, status FROM app_user WHERE id = %s", (user_id,))
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "usuário não encontrado")
    if target["status"] == "disabled":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "usuário desativado — reative antes de convidar")

    token = await auth.issue_invite(user_id, purpose, admin.id)
    await audit(admin, f"user.{purpose}.issue", "app_user", user_id,
                ip=auth.client_ip(request))
    return {"email": target["email"], "purpose": purpose, "token": token,
            "expires_in_hours": settings.invite_ttl_hours,
            "aviso": "repasse por canal seguro — é de uso único"}


@router.post("/users/{user_id}/status",
             dependencies=[Depends(require_superuser)])
async def set_user_status(user_id: str, status_: Literal["active", "disabled"],
                          user: CurrentUser, request: Request):
    if user_id == user.id and status_ == "disabled":
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "não dá pra desativar a si mesmo")
    async with db.connection() as conn:
        async with conn.transaction():
            row = await db.fetch_one(
                "UPDATE app_user SET status = %s,"
                " disabled_at = CASE WHEN %s = 'disabled' THEN now()"
                "                    ELSE NULL END"
                " WHERE id = %s RETURNING id, email, status",
                (status_, status_, user_id), conn=conn)
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    "usuário não encontrado")
            # Desativar precisa cortar o acesso agora, não quando a sessão
            # expirar.
            if status_ == "disabled":
                await auth.revoke_all_sessions(user_id, conn=conn)
            await audit(user, "user.status", "app_user", user_id,
                        after={"status": status_},
                        ip=auth.client_ip(request), conn=conn)
    return row


# --------------------------------------------------------------------------- #
# Membros do projeto
# --------------------------------------------------------------------------- #

@router.get("/projects/{project_slug}/members",
            dependencies=[Depends(require_role("viewer"))])
async def list_members(project_slug: Annotated[str, Path()]):
    return await db.fetch_all(
        "SELECT u.id, u.email, u.display_name, m.role, m.granted_at"
        "  FROM project_member m"
        "  JOIN app_user u ON u.id = m.user_id"
        "  JOIN project p ON p.id = m.project_id"
        " WHERE p.slug = %s ORDER BY u.display_name", (project_slug,))


@router.put("/projects/{project_slug}/members")
async def upsert_member(body: MemberIn, project_slug: Annotated[str, Path()],
                        request: Request,
                        actor: Annotated[Principal, Depends(require_role("admin"))]):
    proj = await db.fetch_one("SELECT id FROM project WHERE slug = %s",
                              (project_slug,))
    target = await db.fetch_one(
        "SELECT id FROM app_user WHERE lower(email) = %s", (body.email.lower(),))
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "usuário não encontrado")

    # Só owner concede owner: senão um admin poderia se promover.
    actor_role = await project_role(project_slug, actor)
    if ROLE_RANK[body.role] >= ROLE_RANK["owner"] and actor_role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "só um owner pode conceder o papel 'owner'")

    before = await db.fetch_one(
        "SELECT role FROM project_member WHERE project_id=%s AND user_id=%s",
        (proj["id"], target["id"]))

    # Rebaixar owner é tão sensível quanto removê-lo — e `remove_member` já
    # protegia o invariante "o projeto não fica sem owner". Sem esta guarda, um
    # admin rebaixava o único owner para viewer e o projeto ficava órfão:
    # ninguém mais podia arquivá-lo nem conceder owner, e o próprio admin
    # passava a ser o papel máximo.
    if before and before["role"] == "owner" and body.role != "owner":
        if actor_role != "owner":
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "só um owner pode rebaixar outro owner")
        n = await db.fetch_one(
            "SELECT count(*) AS n FROM project_member"
            " WHERE project_id = %s AND role = 'owner'", (proj["id"],))
        if n["n"] <= 1:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "o projeto ficaria sem owner")
    async with db.connection() as conn:
        async with conn.transaction():
            await db.execute(
                "INSERT INTO project_member (project_id, user_id, role,"
                " granted_by) VALUES (%s,%s,%s,%s)"
                " ON CONFLICT (project_id, user_id) DO UPDATE"
                " SET role = EXCLUDED.role, granted_by = EXCLUDED.granted_by,"
                "     granted_at = now()",
                (proj["id"], target["id"], body.role, actor.id), conn=conn)
            await audit(actor, "member.grant", "project_member",
                        str(target["id"]), project_id=str(proj["id"]),
                        before=dict(before) if before else None,
                        after={"role": body.role},
                        ip=auth.client_ip(request), conn=conn)
    return {"email": body.email, "role": body.role}


@router.delete("/projects/{project_slug}/members/{user_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(project_slug: Annotated[str, Path()], user_id: str,
                        request: Request,
                        actor: Annotated[Principal, Depends(require_role("admin"))]):
    proj = await db.fetch_one("SELECT id FROM project WHERE slug = %s",
                              (project_slug,))
    # Um projeto sem owner fica órfão: ninguém pode mais gerenciar membros.
    row = await db.fetch_one(
        "SELECT role FROM project_member WHERE project_id=%s AND user_id=%s",
        (proj["id"], user_id))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "membro não encontrado")
    if row["role"] == "owner":
        n = await db.fetch_one(
            "SELECT count(*) AS n FROM project_member"
            " WHERE project_id = %s AND role = 'owner'", (proj["id"],))
        if n["n"] <= 1:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "o projeto ficaria sem owner")
    async with db.connection() as conn:
        async with conn.transaction():
            await db.execute(
                "DELETE FROM project_member WHERE project_id=%s AND user_id=%s",
                (proj["id"], user_id), conn=conn)
            await audit(actor, "member.revoke", "project_member", user_id,
                        project_id=str(proj["id"]), before=dict(row),
                        ip=auth.client_ip(request), conn=conn)
