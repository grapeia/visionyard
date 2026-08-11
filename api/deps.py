"""Dependências do FastAPI: quem é o principal e o que ele pode fazer.

RBAC é escopado por projeto (tabela project_member). Um usuário pode ser
annotator num projeto e engineer noutro — o caso concreto é gente terceirizada
que só rotula e não deve enxergar modelos nem disparar treino.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Path, Request, status

from . import db
from .auth import (
    AuthError,
    DevicePrincipal,
    Principal,
    principal_from_request,
    resolve_device_token,
)

# Papéis são lineares: quem pode mais pode menos.
ROLE_RANK = {"viewer": 0, "annotator": 1, "engineer": 2, "admin": 3, "owner": 4}


async def current_user(request: Request) -> Principal:
    try:
        return await principal_from_request(request)
    except AuthError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e),
                            headers={"WWW-Authenticate": "Bearer"}) from e


CurrentUser = Annotated[Principal, Depends(current_user)]


async def project_role(project_slug: str, user: Principal) -> str | None:
    if user.is_superuser:
        return "owner"
    row = await db.fetch_one(
        "SELECT m.role FROM project_member m"
        "  JOIN project p ON p.id = m.project_id"
        " WHERE p.slug = %s AND m.user_id = %s", (project_slug, user.id))
    return row["role"] if row else None


def require_role(minimum: str):
    """Dependência que exige papel >= `minimum` no projeto da rota.

    Usa 404 (e não 403) quando o usuário não é membro: responder 403 revelaria
    que o projeto existe para quem não deveria saber.
    """
    if minimum not in ROLE_RANK:
        raise ValueError(f"papel desconhecido: {minimum}")

    async def dep(user: CurrentUser,
                  project_slug: Annotated[str, Path()]) -> Principal:
        role = await project_role(project_slug, user)
        if role is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "projeto não encontrado")
        # Arquivar sumia da listagem mas não impedia nada: dava para importar,
        # congelar versão e trocar papéis num projeto encerrado. Leitura
        # continua permitida — é o ponto de arquivar em vez de apagar.
        if ROLE_RANK[minimum] > ROLE_RANK["viewer"]:
            arq = await db.fetch_one(
                "SELECT archived_at FROM project WHERE slug = %s",
                (project_slug,))
            if arq and arq["archived_at"] is not None:
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    "projeto arquivado — só leitura")
        if ROLE_RANK[role] < ROLE_RANK[minimum]:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"requer papel '{minimum}' ou superior neste projeto "
                f"(seu papel: '{role}')")
        return user

    return dep


async def require_superuser(user: CurrentUser) -> Principal:
    if not user.is_superuser:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "requer administrador da instalação")
    return user


async def current_device(request: Request) -> DevicePrincipal:
    """Autentica o agente por `X-Agent-Token`.

    Header próprio, não `Authorization: Bearer`, para não confundir os dois
    principais: token de usuário e token de máquina chegam por portas
    diferentes e nunca são intercambiáveis.
    """
    token = request.headers.get("x-agent-token", "").strip()
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "token de agente ausente")
    try:
        return await resolve_device_token(token)
    except AuthError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e


CurrentDevice = Annotated[DevicePrincipal, Depends(current_device)]
