"""Parâmetros da instalação (superusuário).

Só HTTP: o corpo, o `response_model`, o RBAC e o código de status. Quais
parâmetros existem, como se validam e quem vence quando o ambiente também
define — tudo em `services/parameters.py`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel

from .. import auth
from ..auth import Principal
from ..deps import require_superuser
from ..services import parameters as svc

router = APIRouter(tags=["parameters"])


class Opcao(BaseModel):
    valor: str
    rotulo: str


class Parametro(BaseModel):
    chave: str
    rotulo: str
    ajuda: str
    tipo: str
    # lista, texto ou inteiro conforme `tipo`; a validação é do serviço.
    valor: Any
    origem: str              # "ambiente" | "tela" | "padrão"
    opcoes: list[Opcao]      # só quando `tipo` é "opcoes"
    variavel: str            # o nome que trava o campo, para a tela citar
    alterado_em: datetime | None
    alterado_por: str | None


class ValorIn(BaseModel):
    valor: Any


class BucketDiagnosticado(BaseModel):
    bucket: str
    papel: str               # "imagens" | "modelos"
    faltando: list[str]
    erro: str | None


class Diagnostico(BaseModel):
    conta: str | None
    origem: str | None
    assina: bool
    erro: str | None
    buckets: list[BucketDiagnosticado]


@router.get("/parameters", response_model=list[Parametro],
            dependencies=[Depends(require_superuser)])
async def listar():
    return await svc.listar()


@router.get("/parameters/diagnostico", response_model=Diagnostico,
            dependencies=[Depends(require_superuser)])
async def diagnosticar():
    """O que a credencial da API consegue fazer nos buckets que valem agora."""
    return await svc.diagnosticar()


@router.put("/parameters/{chave}", response_model=Parametro)
async def definir(chave: Annotated[str, Path()], body: ValorIn,
                  request: Request,
                  admin: Annotated[Principal, Depends(require_superuser)]):
    return await svc.definir(admin, chave, body.valor, auth.client_ip(request))


@router.delete("/parameters/{chave}", response_model=Parametro)
async def restaurar(chave: Annotated[str, Path()], request: Request,
                    admin: Annotated[Principal, Depends(require_superuser)]):
    """Volta ao padrão, apagando o que a tela gravou."""
    return await svc.restaurar(admin, chave, auth.client_ip(request))
