"""Cadastro de máquinas que executam jobs (agentes de treino e inferência).

Device é infraestrutura da instalação, não de um projeto — os servidores de GPU e os
dispositivos de borda servem a todos os projetos. Por isso só o superusuário gerencia.

`capabilities` é o que decide o roteamento: um job declara o que precisa
(`requires`, ex. `{"train": true}`) e o claim só entrega a device cujo
`capabilities` contém aquilo. Marcar o dispositivo de produção como `draining` o tira
do pool sem apagá-lo — ele continua servindo a linha, mas não pega pré-tag.
"""
from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, Field, field_validator

from .. import auth, db
from ..audit import audit
from ..auth import Principal
from ..deps import CurrentUser, require_superuser
from ..security import new_token, token_digest
from .projects import SLUG_RE

router = APIRouter(tags=["devices"])


class DeviceIn(BaseModel):
    slug: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    device_class: str = Field(min_length=1, max_length=64)  # 'gpu-server'
    # {"train": true, "gpu": "...", "vram_gb": 16} ou
    # {"infer": true, "gpu": "...", "cuda": "12.2"}
    capabilities: dict = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not SLUG_RE.match(v):
            raise ValueError("slug deve ser minúsculo, com hífens")
        return v


def _emit_token(device_id: str) -> tuple[str, bytes]:
    tok = new_token()
    return tok, token_digest(tok)


@router.get("/devices", dependencies=[Depends(require_superuser)])
async def list_devices():
    return await db.fetch_all(
        "SELECT id, slug, name, device_class, capabilities, labels, status,"
        "       last_heartbeat, registered_at,"
        "       (SELECT count(*) FROM job j WHERE j.device_id = device.id"
        "         AND j.status = 'running') AS rodando"
        "  FROM device ORDER BY slug")


@router.get("/deploy-devices")
async def deploy_devices(user: CurrentUser):
    """Máquinas que podem receber uma implantação. Visível a qualquer usuário
    autenticado (o engenheiro precisa escolher o alvo ao implantar), mas só
    nome/classe/estado — capabilities e token ficam com o superusuário."""
    return await db.fetch_all(
        "SELECT slug, name, device_class, status FROM device"
        " WHERE status <> 'disabled' ORDER BY slug")


@router.post("/devices", status_code=status.HTTP_201_CREATED)
async def create_device(body: DeviceIn, request: Request,
                        admin: Annotated[Principal, Depends(require_superuser)]):
    dup = await db.fetch_one("SELECT id FROM device WHERE slug = %s",
                             (body.slug,))
    if dup:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"já existe device '{body.slug}'")
    tok, digest = _emit_token(body.slug)
    async with db.connection() as conn:
        async with conn.transaction():
            row = await db.fetch_row(
                "INSERT INTO device (slug, name, device_class, capabilities,"
                " labels, token_sha256, status, registered_by)"
                " VALUES (%s,%s,%s,%s::jsonb,%s,%s,'offline',%s)"
                " RETURNING id, slug, name, device_class, capabilities,"
                " labels, status",
                (body.slug, body.name, body.device_class,
                 json.dumps(body.capabilities), body.labels, digest,
                 admin.id), conn=conn)
            await audit(admin, "device.create", "device", str(row["id"]),
                        after={"slug": body.slug,
                               "class": body.device_class},
                        ip=auth.client_ip(request), conn=conn)
    # O token em claro existe só aqui — quem cadastra copia para o .env do agente.
    return {**row, "token": tok,
            "aviso": "guarde o token: ele não é mostrado de novo"}


@router.post("/devices/{slug}/token")
async def rotate_token(slug: Annotated[str, Path()], request: Request,
                       admin: Annotated[Principal, Depends(require_superuser)]):
    dev = await db.fetch_one("SELECT id FROM device WHERE slug = %s", (slug,))
    if dev is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device não encontrado")
    tok, digest = _emit_token(slug)
    async with db.connection() as conn:
        async with conn.transaction():
            await db.execute(
                "UPDATE device SET token_sha256 = %s WHERE id = %s",
                (digest, dev["id"]), conn=conn)
            await audit(admin, "device.token.rotate", "device",
                        str(dev["id"]), ip=auth.client_ip(request), conn=conn)
    return {"slug": slug, "token": tok,
            "aviso": "o token anterior deixou de valer agora"}


@router.delete("/devices/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(slug: Annotated[str, Path()], request: Request,
                        admin: Annotated[Principal, Depends(require_superuser)]):
    """Exclui uma máquina. Recusa se ela tem histórico de jobs.

    `job.device_id` guarda quem EXECUTOU cada treino — apagar a máquina apagaria
    esse rastro (e o FK bloquearia). Para retirar uma máquina em uso, o caminho
    é `disabled` (mantém o histórico e invalida o token); excluir é só para uma
    máquina cadastrada por engano, que nunca rodou nada."""
    dev = await db.fetch_one("SELECT id FROM device WHERE slug = %s", (slug,))
    if dev is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device não encontrado")
    n = await db.fetch_row("SELECT count(*) AS n FROM job WHERE device_id = %s",
                           (dev["id"],))
    if n["n"] > 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"máquina tem {n['n']} job(s) no histórico — desative em vez de "
            f"excluir, para preservar quem executou o quê")
    async with db.connection() as conn:
        async with conn.transaction():
            await db.execute("DELETE FROM device WHERE id = %s",
                             (dev["id"],), conn=conn)
            await audit(admin, "device.delete", "device", str(dev["id"]),
                        before={"slug": slug}, ip=auth.client_ip(request),
                        conn=conn)


@router.post("/devices/{slug}/status")
async def set_status(slug: Annotated[str, Path()],
                     estado: Literal["idle", "draining", "disabled"],
                     request: Request,
                     admin: Annotated[Principal, Depends(require_superuser)]):
    """`draining` tira do pool sem apagar; `disabled` também invalida o token.
    `busy`/`offline` são do agente (heartbeat), não se define à mão."""
    async with db.connection() as conn:
        async with conn.transaction():
            row = await db.fetch_one(
                "UPDATE device SET status = %s WHERE slug = %s"
                " RETURNING id, slug, status", (estado, slug), conn=conn)
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    "device não encontrado")
            await audit(admin, "device.status", "device", str(row["id"]),
                        after={"status": estado},
                        ip=auth.client_ip(request), conn=conn)
    return row
