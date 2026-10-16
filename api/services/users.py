"""Usuários da instalação e papéis por projeto.

Duas coisas diferentes moram aqui porque são a mesma tela: o cadastro de quem
tem conta (superusuário) e quem pode o quê em cada projeto (admin do projeto).

O invariante que governa a segunda metade é **o projeto nunca fica sem owner**:
sem owner ninguém arquiva o projeto nem concede owner, e o papel máximo dele
passa a ser `admin` — não há como sair desse estado de dentro do produto. Toda
escrita de membro é serializada por um lock consultivo por projeto; ver
`_travar_membros`.

Segue a forma do item 15 do PLANO: toda função que fala com o banco recebe
`conn`, quem abre a conexão é a função pública, e `conn.transaction()` vai no
MESMO `async with` da conexão.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

from .. import auth, db
from ..audit import audit
from ..auth import Principal
from ..deps import ROLE_RANK, project_role
from ..security import hash_password
from .projects import projeto_ou_404


async def _travar_membros(conn: db.Conn, project_id: str) -> None:
    """Serializa as escritas de membro deste projeto até o fim da transação.

    A guarda "o projeto ficaria sem owner" é um leia-decida-escreva, e contar
    dentro da transação não a salva: em READ COMMITTED cada comando pega
    snapshot novo, então duas remoções simultâneas leem 2 antes de qualquer uma
    gravar, as duas passam pela guarda e o projeto fica órfão. O que resolve é
    serializar — mesmo recurso do append de schema e do congelamento.
    """
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                       (f"members:{project_id}",))


async def _quantos_owners(conn: db.Conn, project_id: str) -> int:
    row = await db.fetch_row(
        "SELECT count(*) AS n FROM project_member"
        " WHERE project_id = %s AND role = 'owner'", (project_id,), conn=conn)
    return row["n"]


# --------------------------------------------------------------------------- #
# Cadastro
# --------------------------------------------------------------------------- #

async def listar() -> list[dict[str, Any]]:
    async with db.connection() as conn:
        return await db.fetch_all(
            "SELECT id, email, display_name, status, is_superuser, created_at,"
            "       last_login_at FROM app_user ORDER BY display_name",
            conn=conn)


async def criar(admin: Principal, *, email: str, display_name: str,
                password: str | None, is_superuser: bool,
                ip: str | None) -> dict[str, Any]:
    """Sem senha o usuário nasce `invited` e a define no primeiro acesso."""
    async with db.connection() as conn, conn.transaction():
        # `ON CONFLICT` e não ler-antes-de-inserir: em READ COMMITTED dois
        # cadastros simultâneos do mesmo email leem "não existe" os dois, e o
        # segundo estoura no `app_user_email_key` — 500 no lugar do 409 que
        # este handler existe para dar. Quem decide é o índice, que é o único
        # que enxerga as duas transações.
        row = await db.fetch_one(
            "INSERT INTO app_user (email, display_name, password_hash,"
            " status, is_superuser, created_by) VALUES (%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (lower(email)) DO NOTHING"
            " RETURNING id, email, display_name, status, is_superuser",
            (email.lower(), display_name,
             hash_password(password) if password else None,
             "active" if password else "invited", is_superuser, admin.id),
            conn=conn)
        if row is None:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "email já cadastrado")
        await audit(admin, "user.create", "app_user", str(row["id"]),
                    after={"email": row["email"], "superuser": is_superuser},
                    ip=ip, conn=conn)
    return row


async def emitir_convite(admin: Principal, user_id: UUID, purpose: str,
                         ip: str | None) -> dict[str, Any]:
    """Convite de uso único; emitir um novo invalida o anterior.

    O token volta na resposta em vez de ir por email porque ainda não há
    serviço de email configurado — quem administra repassa pelo canal que usar.
    """
    async with db.connection() as conn, conn.transaction():
        alvo = await db.fetch_one(
            "SELECT id, email, status FROM app_user WHERE id = %s",
            (user_id,), conn=conn)
        if alvo is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "usuário não encontrado")
        if alvo["status"] == "disabled":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "usuário desativado — reative antes de convidar")
        token = await auth.issue_invite(str(user_id), purpose, admin.id,
                                        conn=conn)
        await audit(admin, f"user.{purpose}.issue", "app_user", str(user_id),
                    ip=ip, conn=conn)
    return {"email": alvo["email"], "purpose": purpose, "token": token}


async def definir_status(actor: Principal, user_id: UUID, novo_status: str,
                         ip: str | None) -> dict[str, Any]:
    if str(user_id) == actor.id and novo_status == "disabled":
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "não dá pra desativar a si mesmo")
    async with db.connection() as conn, conn.transaction():
        row = await db.fetch_one(
            "UPDATE app_user SET status = %s,"
            " disabled_at = CASE WHEN %s = 'disabled' THEN now()"
            "                    ELSE NULL END"
            " WHERE id = %s RETURNING id, email, status",
            (novo_status, novo_status, user_id), conn=conn)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "usuário não encontrado")
        # Desativar precisa cortar o acesso agora, não quando a sessão expirar.
        if novo_status == "disabled":
            await auth.revoke_all_sessions(str(user_id), conn=conn)
        await audit(actor, "user.status", "app_user", str(user_id),
                    after={"status": novo_status}, ip=ip, conn=conn)
    return row


# --------------------------------------------------------------------------- #
# Membros do projeto
# --------------------------------------------------------------------------- #

async def listar_membros(project_slug: str) -> list[dict[str, Any]]:
    async with db.connection() as conn:
        return await db.fetch_all(
            "SELECT u.id, u.email, u.display_name, m.role, m.granted_at"
            "  FROM project_member m"
            "  JOIN app_user u ON u.id = m.user_id"
            "  JOIN project p ON p.id = m.project_id"
            " WHERE p.slug = %s ORDER BY u.display_name",
            (project_slug,), conn=conn)


async def candidatos_a_membro(project_slug: str) -> list[dict[str, Any]]:
    """Contas que o admin do projeto pode incluir: as que ainda não são membros.

    Desativada fica de fora — incluí-la seria conceder papel a quem não entra.
    Convidada entra: o papel espera o primeiro acesso dela.
    """
    async with db.connection() as conn:
        proj = await projeto_ou_404(conn, project_slug)
        return await db.fetch_all(
            "SELECT u.id, u.email, u.display_name FROM app_user u"
            " WHERE u.status <> 'disabled'"
            "   AND NOT EXISTS (SELECT 1 FROM project_member m"
            "                    WHERE m.user_id = u.id AND m.project_id = %s)"
            " ORDER BY u.display_name, u.email",
            (proj["id"],), conn=conn)


async def definir_membro(project_slug: str, actor: Principal, *, email: str,
                         role: str, ip: str | None) -> dict[str, Any]:
    async with db.connection() as conn, conn.transaction():
        proj = await projeto_ou_404(conn, project_slug)
        await _travar_membros(conn, str(proj["id"]))

        alvo = await db.fetch_one(
            "SELECT id FROM app_user WHERE lower(email) = %s",
            (email.lower(),), conn=conn)
        if alvo is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "usuário não encontrado")

        # Só owner concede owner: senão um admin poderia se promover.
        papel_do_ator = await project_role(project_slug, actor, conn=conn)
        if ROLE_RANK[role] >= ROLE_RANK["owner"] and papel_do_ator != "owner":
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "só um owner pode conceder o papel 'owner'")

        antes = await db.fetch_one(
            "SELECT role FROM project_member"
            " WHERE project_id = %s AND user_id = %s",
            (proj["id"], alvo["id"]), conn=conn)

        # Rebaixar owner é tão sensível quanto removê-lo, e as duas portas
        # carregam as mesmas duas guardas — quem pode, e o que sobra depois.
        # Sem esta, um admin rebaixava o único owner para viewer e o projeto
        # ficava órfão: ninguém mais podia arquivá-lo nem conceder owner, e o
        # próprio admin passava a ser o papel máximo.
        if antes and antes["role"] == "owner" and role != "owner":
            if papel_do_ator != "owner":
                raise HTTPException(status.HTTP_403_FORBIDDEN,
                                    "só um owner pode rebaixar outro owner")
            if await _quantos_owners(conn, str(proj["id"])) <= 1:
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    "o projeto ficaria sem owner")

        await db.execute(
            "INSERT INTO project_member (project_id, user_id, role,"
            " granted_by) VALUES (%s,%s,%s,%s)"
            " ON CONFLICT (project_id, user_id) DO UPDATE"
            " SET role = EXCLUDED.role, granted_by = EXCLUDED.granted_by,"
            "     granted_at = now()",
            (proj["id"], alvo["id"], role, actor.id), conn=conn)
        await audit(actor, "member.grant", "project_member", str(alvo["id"]),
                    project_id=str(proj["id"]),
                    before=dict(antes) if antes else None,
                    after={"role": role}, ip=ip, conn=conn)
    return {"email": email, "role": role}


async def remover_membro(project_slug: str, actor: Principal, *,
                         user_id: UUID, ip: str | None) -> None:
    async with db.connection() as conn, conn.transaction():
        proj = await projeto_ou_404(conn, project_slug)
        await _travar_membros(conn, str(proj["id"]))

        # Um projeto sem owner fica órfão: ninguém pode mais gerenciar membros.
        row = await db.fetch_one(
            "SELECT role FROM project_member"
            " WHERE project_id = %s AND user_id = %s",
            (proj["id"], user_id), conn=conn)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "membro não encontrado")
        if row["role"] == "owner":
            # A mesma guarda de `definir_membro`, e pela mesma razão. Sem ela
            # aqui, o 403 de lá é contornável pela porta do lado: o admin
            # remove o owner e o recadastra como viewer, chegando ao mesmo
            # estado em duas requisições e sem owner nenhum concordar.
            if await project_role(project_slug, actor, conn=conn) != "owner":
                raise HTTPException(status.HTTP_403_FORBIDDEN,
                                    "só um owner pode remover outro owner")
            if await _quantos_owners(conn, str(proj["id"])) <= 1:
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    "o projeto ficaria sem owner")

        await db.execute(
            "DELETE FROM project_member"
            " WHERE project_id = %s AND user_id = %s",
            (proj["id"], user_id), conn=conn)
        await audit(actor, "member.revoke", "project_member", str(user_id),
                    project_id=str(proj["id"]), before=dict(row), ip=ip,
                    conn=conn)
