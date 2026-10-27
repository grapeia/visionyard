"""Permissões por papel (superusuário).

Só HTTP: o corpo, o `response_model`, o RBAC e o código de status. Quais ações
existem, o padrão de cada uma e como a exceção gravada se combina com ele —
tudo em `services/permissions.py`.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel

from .. import auth
from ..auth import Principal
from ..deps import require_superuser
from ..services import permissions as svc

router = APIRouter(tags=["permissions"])


class Papel(BaseModel):
    chave: str
    nome: str


class Celula(BaseModel):
    permitido: bool
    alterado: bool           # difere do padrão do catálogo


class Acao(BaseModel):
    chave: str
    pagina: str
    secao: str
    nome: str
    ajuda: str
    padrao: str              # o papel mínimo quando nada foi alterado
    sensivel: bool
    papeis: dict[str, Celula]


class Matriz(BaseModel):
    papeis: list[Papel]
    acoes: list[Acao]


class PermitidoIn(BaseModel):
    permitido: bool


@router.get("/permissions", response_model=Matriz,
            dependencies=[Depends(require_superuser)])
async def matriz():
    return await svc.matriz()


@router.put("/permissions/{acao}/{papel}", response_model=Acao)
async def definir(acao: Annotated[str, Path()], papel: Annotated[str, Path()],
                  body: PermitidoIn, request: Request,
                  admin: Annotated[Principal, Depends(require_superuser)]):
    return await svc.definir(admin, acao, papel, body.permitido,
                             ip=auth.client_ip(request))


@router.delete("/permissions/{acao}/{papel}", response_model=Acao)
async def restaurar(acao: Annotated[str, Path()], papel: Annotated[str, Path()],
                    request: Request,
                    admin: Annotated[Principal, Depends(require_superuser)]):
    return await svc.definir(admin, acao, papel, None,
                             ip=auth.client_ip(request))
