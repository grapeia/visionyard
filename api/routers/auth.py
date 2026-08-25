"""Rotas de autenticação: login, logout, quem sou eu, trocar senha.

Só HTTP: o corpo, o `response_model`, o cookie e o código de status. A regra
mora em `api/auth.py`, que é o serviço deste domínio — não há
`services/auth.py`; o porquê está lá.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, EmailStr, Field

from .. import auth
from ..config import settings
from ..deps import CurrentUser

router = APIRouter(tags=["auth"])


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class MeOut(BaseModel):
    id: str
    email: str
    display_name: str
    is_superuser: bool
    via: str


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=settings.password_min_length)


class InviteAcceptIn(BaseModel):
    token: str
    password: str = Field(min_length=settings.password_min_length)


class TokenCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class TokenDeApi(BaseModel):
    """`prefix` é o começo do valor: serve para reconhecer o token na lista sem
    guardá-lo — o banco só tem o sha256."""
    id: UUID
    name: str
    prefix: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None


class TokenCriado(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    # Devolvido uma única vez: o banco guarda só o sha256.
    token: str
    aviso: str


class SessaoAberta(BaseModel):
    id: UUID
    issued_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    ip: str | None
    user_agent: str | None
    # A sessão desta requisição — a tela não oferece "encerrar" para ela.
    current: bool


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.cookie_name, token,
        max_age=settings.session_max_days * 86400,
        httponly=True,                      # inacessível a JS: mitiga XSS
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # lax: mitiga CSRF na navegação
        path="/")


@router.post("/login", response_model=MeOut)
async def login(body: LoginIn, request: Request, response: Response):
    ip = auth.client_ip(request)
    try:
        user = await auth.authenticate(body.email, body.password, ip)
    except auth.ThrottledError as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(e)) from e
    except auth.AuthError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e

    token = await auth.create_session(user["id"], ip,
                                      request.headers.get("user-agent"))
    _set_cookie(response, token)
    return MeOut(id=str(user["id"]), email=user["email"],
                 display_name=user["display_name"],
                 is_superuser=user["is_superuser"], via="session")


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(user: CurrentUser, response: Response):
    if user.session_id:
        await auth.revoke_session(user.session_id)
    response.delete_cookie(settings.cookie_name, path="/")


@router.get("/me", response_model=MeOut)
async def me(user: CurrentUser):
    return MeOut(id=user.id, email=user.email, display_name=user.display_name,
                 is_superuser=user.is_superuser, via=user.via)


@router.post("/invite/accept", response_model=MeOut)
async def accept_invite(body: InviteAcceptIn, request: Request,
                        response: Response):
    """Define a primeira senha (ou redefine) a partir de um convite.

    Rota pública por necessidade: quem a usa ainda não consegue autenticar. A
    proteção é o token de uso único com validade.
    """
    try:
        user = await auth.accept_invite(body.token, body.password,
                                        ip=auth.client_ip(request))
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    except auth.AuthError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    token = await auth.create_session(user["id"], auth.client_ip(request),
                                      request.headers.get("user-agent"))
    _set_cookie(response, token)
    return MeOut(id=user["id"], email=user["email"],
                 display_name=user["display_name"], is_superuser=False,
                 via="session")


@router.get("/me/tokens", response_model=list[TokenDeApi])
async def list_my_tokens(user: CurrentUser):
    return await auth.listar_tokens(user)


@router.post("/me/tokens", response_model=TokenCriado,
             status_code=status.HTTP_201_CREATED)
async def create_my_token(body: TokenCreateIn, user: CurrentUser,
                          request: Request):
    """Cria token de API que age COMO este usuário (herda seus papéis).

    O valor é devolvido uma única vez — o banco guarda só o sha256.
    """
    novo = await auth.criar_token(user, name=body.name,
                                  expires_in_days=body.expires_in_days,
                                  ip=auth.client_ip(request))
    return {**novo,
            "aviso": "guarde agora — não é possível recuperá-lo depois"}


@router.delete("/me/tokens/{token_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def revoke_my_token(token_id: Annotated[UUID, Path()],
                          user: CurrentUser, request: Request):
    await auth.revogar_token(user, token_id, auth.client_ip(request))


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(body: PasswordChangeIn, user: CurrentUser,
                          request: Request, response: Response):
    """Trocar a senha derruba todo acesso anterior — sessões e tokens de API —
    e devolve uma sessão nova, para quem trocou não ser deslogado."""
    await auth.trocar_senha(user, atual=body.current_password,
                            nova=body.new_password,
                            ip=auth.client_ip(request))
    token = await auth.create_session(user.id, auth.client_ip(request),
                                      request.headers.get("user-agent"))
    _set_cookie(response, token)


@router.get("/me/sessions", response_model=list[SessaoAberta])
async def my_sessions(user: CurrentUser):
    return await auth.listar_sessoes(user)


@router.delete("/me/sessions/{session_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def revoke_my_session(session_id: Annotated[UUID, Path()],
                            user: CurrentUser):
    await auth.revogar_sessao(user, session_id)
