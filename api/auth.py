"""Autenticação: login, sessão e identificação do principal da requisição.

Sessão é server-side (tabela user_session) e não JWT. JWT não é revogável antes
de expirar, então desativar um usuário não teria efeito imediato — inaceitável
num sistema que controla o que roda em máquina de produção. Com tabela, revogar
é um UPDATE.

Dois tipos de principal chegam pela mesma porta:
  * user   — cookie de sessão (navegador) ou token de API (script)
  * device — token de device (agente de treino), tratado em api/devices.py

A distinção importa pra atribuição: job.created_by é a pessoa, job.device_id é
a máquina.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Request
from psycopg import AsyncConnection

from . import db
from .audit import audit
from .config import settings
from .security import (
    dummy_verify,
    hash_password,
    needs_rehash,
    new_token,
    token_digest,
    verify_password,
)


@dataclass(frozen=True)
class Principal:
    kind: str                 # 'user'
    id: str
    email: str
    display_name: str
    is_superuser: bool
    session_id: str | None = None
    via: str = "session"      # 'session' | 'api_token'


@dataclass(frozen=True)
class DevicePrincipal:
    """O agente de treino como principal. Separado de Principal de propósito:
    máquina não é pessoa, e a atribuição de job guarda os dois (created_by é
    quem pediu, device_id é quem executou)."""
    kind: str                 # sempre 'device'
    id: str
    slug: str
    name: str
    capabilities: dict


class AuthError(Exception):
    """Credencial inválida/ausente — vira 401 no router."""


class ThrottledError(Exception):
    """Tentativas demais — vira 429."""


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #

async def check_throttle(email: str, ip: str | None) -> None:
    """Teto duro por IP; por email, só backoff progressivo.

    Bloquear por email com trava dura protegeria contra força bruta mas criaria
    um lockout dirigido: quem souber o email de alguém o tranca fora só errando
    a senha de propósito. Então o teto fica no IP (que é o que um atacante
    precisa variar para escalar) e o email ganha um atraso que dobra a cada
    falha, com teto baixo — atrasa o ataque distribuído e, no pior caso, custa
    ao usuário legítimo alguns segundos de espera.
    """
    since = _now() - timedelta(minutes=settings.login_window_minutes)

    if ip:
        row = await db.fetch_row(
            "SELECT count(*) AS n FROM login_attempt"
            " WHERE NOT successful AND at > %s AND ip = %s", (since, ip))
        if row["n"] >= settings.login_max_attempts:
            raise ThrottledError(
                f"tentativas demais deste endereço — aguarde "
                f"{settings.login_window_minutes} min")

    row = await db.fetch_row(
        "SELECT count(*) AS n, max(at) AS last FROM login_attempt"
        " WHERE NOT successful AND at > %s AND email = %s", (since, email))
    n = row["n"]
    if n < settings.login_backoff_after:
        return
    delay = min(2 ** (n - settings.login_backoff_after + 1),
                settings.login_backoff_max_seconds)
    ready_at = row["last"] + timedelta(seconds=delay)
    if _now() < ready_at:
        wait = int((ready_at - _now()).total_seconds()) + 1
        raise ThrottledError(f"aguarde {wait}s antes de tentar de novo")


async def record_attempt(email: str, ip: str | None, ok: bool) -> None:
    await db.execute(
        "INSERT INTO login_attempt (email, ip, successful) VALUES (%s,%s,%s)",
        (email, ip, ok))


async def authenticate(email: str, password: str, ip: str | None) -> dict:
    """Valida credencial. Erro é sempre o mesmo texto — não revela se o email
    existe, se está desativado ou se a senha está errada."""
    email = email.strip().lower()
    await check_throttle(email, ip)

    user = await db.fetch_one(
        "SELECT id, email, display_name, password_hash, status, is_superuser"
        " FROM app_user WHERE lower(email) = %s", (email,))

    if user is None:
        dummy_verify()                      # equaliza o tempo de resposta
        await record_attempt(email, ip, False)
        raise AuthError("email ou senha inválidos")

    if not verify_password(password, user["password_hash"]):
        await record_attempt(email, ip, False)
        raise AuthError("email ou senha inválidos")

    if user["status"] != "active":
        await record_attempt(email, ip, False)
        raise AuthError("email ou senha inválidos")

    # Custo do argon2 pode ter subido desde o cadastro: aproveita que a senha
    # em claro está em mãos e re-hasheia com os parâmetros atuais.
    if needs_rehash(user["password_hash"]):
        await db.execute("UPDATE app_user SET password_hash = %s WHERE id = %s",
                         (hash_password(password), user["id"]))

    await record_attempt(email, ip, True)
    await db.execute("UPDATE app_user SET last_login_at = now() WHERE id = %s",
                     (user["id"],))
    return user


# --------------------------------------------------------------------------- #
# Sessão
# --------------------------------------------------------------------------- #

async def create_session(user_id: str, ip: str | None,
                         user_agent: str | None) -> str:
    """Cria a sessão e devolve o token em claro — única vez que ele existe."""
    token = new_token()
    await db.execute(
        "INSERT INTO user_session (user_id, token_sha256, expires_at, ip,"
        " user_agent) VALUES (%s, %s, %s, %s, %s)",
        (user_id, token_digest(token),
         _now() + timedelta(days=settings.session_max_days), ip,
         (user_agent or "")[:500]))
    return token


async def resolve_session(token: str) -> Principal:
    row = await db.fetch_one(
        "SELECT s.id AS session_id, s.user_id, s.expires_at, s.last_seen_at,"
        "       u.email, u.display_name, u.status, u.is_superuser"
        "  FROM user_session s JOIN app_user u ON u.id = s.user_id"
        " WHERE s.token_sha256 = %s AND s.revoked_at IS NULL",
        (token_digest(token),))
    if row is None:
        raise AuthError("sessão inválida")

    now = _now()
    if row["expires_at"] <= now:
        raise AuthError("sessão expirada")
    # Expiração por inatividade, além do teto absoluto.
    if row["last_seen_at"] + timedelta(hours=settings.session_idle_hours) <= now:
        await revoke_session(row["session_id"])
        raise AuthError("sessão expirada por inatividade")
    if row["status"] != "active":
        raise AuthError("usuário desativado")

    await db.execute("UPDATE user_session SET last_seen_at = now()"
                     " WHERE id = %s", (row["session_id"],))
    return Principal(kind="user", id=str(row["user_id"]), email=row["email"],
                     display_name=row["display_name"],
                     is_superuser=row["is_superuser"],
                     session_id=str(row["session_id"]), via="session")


async def revoke_session(session_id: str) -> None:
    await db.execute("UPDATE user_session SET revoked_at = now()"
                     " WHERE id = %s AND revoked_at IS NULL", (session_id,))


async def revoke_all_sessions(user_id: str, *,
                              conn: AsyncConnection | None = None) -> None:
    """Usado ao trocar senha ou desativar usuário.

    Aceita `conn` porque quem chama costuma estar derrubando vários acessos de
    uma vez: revogar as sessões sem revogar os tokens deixaria o acesso de pé.
    """
    await db.execute("UPDATE user_session SET revoked_at = now()"
                     " WHERE user_id = %s AND revoked_at IS NULL", (user_id,),
                     conn=conn)


# --------------------------------------------------------------------------- #
# Token de API (script agindo como um usuário)
# --------------------------------------------------------------------------- #

async def resolve_api_token(token: str) -> Principal:
    row = await db.fetch_one(
        "SELECT t.id, t.user_id, t.expires_at, u.email, u.display_name,"
        "       u.status, u.is_superuser"
        "  FROM api_token t JOIN app_user u ON u.id = t.user_id"
        " WHERE t.token_sha256 = %s AND t.revoked_at IS NULL",
        (token_digest(token),))
    if row is None:
        raise AuthError("token inválido")
    if row["expires_at"] is not None and row["expires_at"] <= _now():
        raise AuthError("token expirado")
    if row["status"] != "active":
        raise AuthError("usuário desativado")
    await db.execute("UPDATE api_token SET last_used_at = now() WHERE id = %s",
                     (row["id"],))
    return Principal(kind="user", id=str(row["user_id"]), email=row["email"],
                     display_name=row["display_name"],
                     is_superuser=row["is_superuser"], via="api_token")


# --------------------------------------------------------------------------- #
# Token de device (agente de treino/inferência)
# --------------------------------------------------------------------------- #

async def resolve_device_token(token: str) -> DevicePrincipal:
    """Identifica o agente pelo token. O banco guarda só o sha256, como em
    qualquer segredo daqui — dump vazado não vira credencial de device."""
    row = await db.fetch_one(
        "SELECT id, slug, name, capabilities, status FROM device"
        " WHERE token_sha256 = %s", (token_digest(token),))
    if row is None:
        raise AuthError("token de device inválido")
    if row["status"] == "disabled":
        raise AuthError("device desativado")
    return DevicePrincipal(kind="device", id=str(row["id"]), slug=row["slug"],
                           name=row["name"], capabilities=row["capabilities"])


# --------------------------------------------------------------------------- #
# Convite / redefinição de senha
# --------------------------------------------------------------------------- #

async def issue_invite(user_id: str, purpose: str,
                       created_by: str | None) -> str:
    """Emite convite de uso único e invalida o anterior do mesmo propósito."""
    if purpose not in ("invite", "password_reset"):
        raise ValueError(f"propósito inválido: {purpose}")
    token = new_token()
    async with db.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE user_invite SET revoked_at = now()"
                " WHERE user_id = %s AND purpose = %s"
                "   AND used_at IS NULL AND revoked_at IS NULL",
                (user_id, purpose))
            await conn.execute(
                "INSERT INTO user_invite (user_id, purpose, token_sha256,"
                " expires_at, created_by) VALUES (%s,%s,%s,%s,%s)",
                (user_id, purpose, token_digest(token),
                 _now() + timedelta(hours=settings.invite_ttl_hours),
                 created_by))
    return token


async def accept_invite(token: str, password: str, *,
                        ip: str | None = None) -> dict:
    """Consome o convite e define a senha. Uso único, em transação.

    A auditoria entra na MESMA transação: aceitar convite é a única escrita que
    o usuário faz antes de existir uma sessão dele, e um registro que comita à
    parte poderia sumir (ou sobrar) sem o fato correspondente.
    """
    if len(password) < settings.password_min_length:
        raise ValueError(
            f"senha precisa de ao menos {settings.password_min_length} "
            f"caracteres")

    async with db.connection() as conn:
        async with conn.transaction():
            # FOR UPDATE: dois POSTs simultâneos com o mesmo token não podem
            # ambos consumir o convite.
            cur = await conn.execute(
                "SELECT i.id, i.user_id, i.expires_at, u.email, u.display_name"
                "  FROM user_invite i JOIN app_user u ON u.id = i.user_id"
                " WHERE i.token_sha256 = %s AND i.used_at IS NULL"
                "   AND i.revoked_at IS NULL FOR UPDATE OF i",
                (token_digest(token),))
            row = await cur.fetchone()
            if row is None:
                raise AuthError("convite inválido ou já utilizado")
            if row["expires_at"] <= _now():
                raise AuthError("convite expirado")

            await conn.execute(
                "UPDATE app_user SET password_hash = %s, status = 'active'"
                " WHERE id = %s", (hash_password(password), row["user_id"]))
            await conn.execute(
                "UPDATE user_invite SET used_at = now() WHERE id = %s",
                (row["id"],))
            # Redefinir senha derruba sessões antigas — se o convite era
            # 'password_reset' por suspeita de vazamento, é o ponto principal.
            await conn.execute(
                "UPDATE user_session SET revoked_at = now()"
                " WHERE user_id = %s AND revoked_at IS NULL", (row["user_id"],))
            # actor=None: quem aceita ainda não é um principal autenticado.
            await audit(None, "user.invite.accept", "app_user",
                        str(row["user_id"]), ip=ip, note="via convite",
                        conn=conn)
    return {"id": str(row["user_id"]), "email": row["email"],
            "display_name": row["display_name"]}


async def principal_from_request(request: Request) -> Principal:
    """Cookie de sessão primeiro; Authorization: Bearer como alternativa."""
    cookie = request.cookies.get(settings.cookie_name)
    if cookie:
        return await resolve_session(cookie)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return await resolve_api_token(header[7:].strip())
    raise AuthError("não autenticado")


def _e_endereco(ip: str | None) -> bool:
    if ip is None:
        return False
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return False
    return True


def _is_trusted_proxy(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for net in settings.trusted_proxies:
        try:
            if addr in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request: Request) -> str | None:
    """IP do cliente, confiando em X-Forwarded-For SÓ atrás de proxy conhecido.

    Confiar no header incondicionalmente anula o teto de login por IP: basta
    mandar um X-Forwarded-For diferente a cada tentativa para nunca acumular
    falhas no mesmo endereço. Como o header é livre para quem chama, ele só vale
    quando o salto imediato está em `settings.trusted_proxies`.

    Com a lista vazia (default), vale sempre o peer real da conexão.

    O retorno é sempre um endereço válido ou None: `audit_log.ip` e
    `login_attempt.ip` são `inet`, e o header é texto livre — o nginx emite
    `unknown` quando não conhece o cliente. Devolver esse texto estourava o cast
    no INSERT, e como a auditoria comita junto com quem escreve, o estouro
    levava a escrita junto (aceitar convite deixava a conta sem senha).
    """
    peer = request.client.host if request.client else None
    if peer is None or not _is_trusted_proxy(peer):
        return peer if _e_endereco(peer) else None

    fwd = request.headers.get("x-forwarded-for")
    if not fwd:
        return peer
    # Da direita para a esquerda: o primeiro salto que não é proxy nosso é o
    # cliente real. O que estiver à esquerda dele foi enviado por ele e pode
    # ser forjado.
    for hop in reversed([h.strip() for h in fwd.split(",") if h.strip()]):
        if not _is_trusted_proxy(hop):
            # Salto ilegível: não dá para dizer quem é o cliente, e o que vem à
            # esquerda foi enviado por quem escreveu isto. Fica o peer, que é o
            # único fato que não veio do header.
            return hop if _e_endereco(hop) else peer
    return peer
