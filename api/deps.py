"""Dependências do FastAPI: quem é o principal e o que ele pode fazer.

RBAC é escopado por projeto (tabela project_member). Um usuário pode ser
annotator num projeto e engineer noutro — o caso concreto é gente terceirizada
que só rotula e não deve enxergar modelos nem disparar treino.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, HTTPException, Path, Request, status

from . import db
from .auth import (
    AuthError,
    DevicePrincipal,
    Principal,
    principal_from_request,
    resolve_device_token,
)
from .services import permissions

ROLE_RANK = permissions.ROLE_RANK


async def current_user(request: Request) -> Principal:
    try:
        return await principal_from_request(request)
    except AuthError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e),
                            headers={"WWW-Authenticate": "Bearer"}) from e


CurrentUser = Annotated[Principal, Depends(current_user)]


async def project_role(project_slug: str, user: Principal,
                       *, conn: db.Conn | None = None) -> str | None:
    """O papel do usuário no projeto, ou `None` se ele não é membro.

    Aceita `conn` pelo mesmo motivo que os helpers de `db.py`: serviço migrado
    (item 15 do PLANO) não alcança o pool por conta própria, e o papel entra na
    resposta ao lado de leituras que precisam vir da mesma conexão.
    """
    if user.is_superuser:
        return "owner"
    async with db.conexao_ou(conn) as c:
        row = await db.fetch_one(
            "SELECT m.role FROM project_member m"
            "  JOIN project p ON p.id = m.project_id"
            " WHERE p.slug = %s AND m.user_id = %s", (project_slug, user.id),
            conn=c)
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
            async with db.connection() as conn:
                arq = await db.fetch_one(
                    "SELECT archived_at FROM project WHERE slug = %s",
                    (project_slug,), conn=conn)
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


def require_action(acao: str):
    """Dependência que exige poder `acao` no projeto da rota.

    É a forma de toda rota que escreve: quem pode o quê é a matriz de
    `services/permissions.py`, que o superusuário edita na tela. Mesmos erros
    de `require_role` e na mesma ordem — e, diferente dela, o superusuário
    também recebe 404 de projeto inexistente, porque a decisão lê a linha do
    projeto de qualquer jeito.
    """
    if acao not in permissions.ACOES:
        raise ValueError(f"ação desconhecida: {acao}")

    async def dep(user: CurrentUser,
                  project_slug: Annotated[str, Path()]) -> Principal:
        async with db.connection() as conn:
            await permissions.decidir(conn, project_slug, user, acao)
        return user

    return dep


async def projeto_ou_404(project_slug: str, colunas: str = "id") -> dict[str, Any]:
    """O projeto da rota, ou 404.

    `require_role` não garante que o projeto existe: superusuário recebe 'owner'
    de `project_role` sem consultar nada, então um slug inexistente atravessava
    a dependência e virava 500 no primeiro `proj["id"]` do handler. Para quem é
    membro o 404 já veio da dependência; esta função fecha o caso dele.

    `colunas` é SQL literal escrito no handler — nunca valor de requisição.
    """
    async with db.connection() as conn:
        row = await db.fetch_one(
            f"SELECT {colunas} FROM project WHERE slug = %s", (project_slug,),
            conn=conn)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "projeto não encontrado")
    return row


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
