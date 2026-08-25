"""Gestão de usuários e de papéis por projeto.

Só HTTP: o corpo, o `response_model`, o RBAC e o código de status. A regra —
inclusive o invariante "o projeto nunca fica sem owner" — mora em
`services/users.py`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request, status
from pydantic import BaseModel, EmailStr, Field

from .. import auth
from ..auth import Principal
from ..config import settings
from ..deps import CurrentUser, require_role, require_superuser
from ..services import users as svc

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


class Usuario(BaseModel):
    id: UUID
    email: EmailStr
    display_name: str
    status: str
    is_superuser: bool
    created_at: datetime
    last_login_at: datetime | None


class UsuarioCriado(BaseModel):
    id: UUID
    email: EmailStr
    display_name: str
    status: str
    is_superuser: bool


class Convite(BaseModel):
    """O token sai na resposta porque ainda não há serviço de email: quem
    administra repassa o link pelo canal que usar. É de uso único."""
    email: EmailStr
    purpose: str
    token: str
    expires_in_hours: int
    aviso: str


class EstadoDoUsuario(BaseModel):
    id: UUID
    email: EmailStr
    status: str


class MembroDoProjeto(BaseModel):
    id: UUID
    email: EmailStr
    display_name: str
    role: str
    granted_at: datetime


class MembroDefinido(BaseModel):
    email: EmailStr
    role: str


@router.get("/users", response_model=list[Usuario],
            dependencies=[Depends(require_superuser)])
async def list_users():
    return await svc.listar()


@router.post("/users", response_model=UsuarioCriado,
             status_code=status.HTTP_201_CREATED)
async def create_user(body: UserCreateIn, request: Request,
                      admin: Annotated[Principal, Depends(require_superuser)]):
    return await svc.criar(admin, email=body.email,
                           display_name=body.display_name,
                           password=body.password,
                           is_superuser=body.is_superuser,
                           ip=auth.client_ip(request))


@router.post("/users/{user_id}/invite", response_model=Convite,
             status_code=status.HTTP_201_CREATED)
async def invite_user(user_id: Annotated[UUID, Path()], request: Request,
                      admin: Annotated[Principal, Depends(require_superuser)],
                      purpose: Literal["invite", "password_reset"] = "invite"):
    """Emite um convite de uso único e devolve o token.

    O token sai na resposta em vez de ir por email porque ainda não há serviço
    de email configurado — quem administra repassa o link pelo canal que usar.
    Emitir um novo invalida o anterior.
    """
    convite = await svc.emitir_convite(admin, user_id, purpose,
                                       auth.client_ip(request))
    return {**convite, "expires_in_hours": settings.invite_ttl_hours,
            "aviso": "repasse por canal seguro — é de uso único"}


@router.post("/users/{user_id}/status", response_model=EstadoDoUsuario,
             dependencies=[Depends(require_superuser)])
async def set_user_status(user_id: Annotated[UUID, Path()],
                          status_: Literal["active", "disabled"],
                          user: CurrentUser, request: Request):
    return await svc.definir_status(user, user_id, status_,
                                    auth.client_ip(request))


# --------------------------------------------------------------------------- #
# Membros do projeto
# --------------------------------------------------------------------------- #

@router.get("/projects/{project_slug}/members",
            response_model=list[MembroDoProjeto],
            dependencies=[Depends(require_role("viewer"))])
async def list_members(project_slug: Annotated[str, Path()]):
    return await svc.listar_membros(project_slug)


@router.put("/projects/{project_slug}/members",
            response_model=MembroDefinido)
async def upsert_member(body: MemberIn, project_slug: Annotated[str, Path()],
                        request: Request,
                        actor: Annotated[Principal, Depends(require_role("admin"))]):
    return await svc.definir_membro(project_slug, actor, email=body.email,
                                    role=body.role,
                                    ip=auth.client_ip(request))


@router.delete("/projects/{project_slug}/members/{user_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(project_slug: Annotated[str, Path()],
                        user_id: Annotated[UUID, Path()],
                        request: Request,
                        actor: Annotated[Principal, Depends(require_role("admin"))]):
    await svc.remover_membro(project_slug, actor, user_id=user_id,
                             ip=auth.client_ip(request))
