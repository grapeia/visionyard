"""Projetos: criação, listagem e ciclo de vida.

Só HTTP: o corpo, o `response_model`, o RBAC e o código de status. A regra —
projeto + owner + schema v1 na mesma transação, e o append de classes sob lock
— mora em `services/projects.py`.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request, status
from pydantic import BaseModel, Field, field_validator

from .. import auth
from ..auth import Principal
from ..deps import CurrentUser, require_role, require_superuser
from ..services import projects as svc

router = APIRouter(tags=["projects"])

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class ClasseNova(BaseModel):
    """Classe com "Dentro de". Em projeto de segmentação, `inside` nomeia a
    classe principal que recorta esta; sem ele a classe é principal. Fica
    fixado na criação, como o nome: o vocabulário é append-only."""
    name: str = Field(min_length=1)
    inside: str | None = None


def _nomes_unicos(v: list[str | ClasseNova], msg: str) -> list[str | ClasseNova]:
    """Nome em branco sai; nome repetido recusa. Vale para a forma curta
    (só o nome) e para a forma com "Dentro de", misturadas na mesma lista."""
    out: list[str | ClasseNova] = []
    for c in v:
        if isinstance(c, str):
            if c.strip():
                out.append(c.strip())
        elif c.name.strip():
            out.append(ClasseNova(name=c.name.strip(),
                                  inside=(c.inside or "").strip() or None))
    if not out:
        raise ValueError("informe ao menos uma classe")
    nomes = [c if isinstance(c, str) else c.name for c in out]
    if len(set(nomes)) != len(nomes):
        raise ValueError(msg)
    return out


def como_dict(c: str | ClasseNova) -> dict[str, str | None]:
    if isinstance(c, str):
        return {"name": c, "inside": None}
    return {"name": c.name, "inside": c.inside}


class ProjectCreateIn(BaseModel):
    slug: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    task_type: Literal["detect", "segment", "classify"] = "detect"
    description: str | None = None
    # As classes do LabelSchema v1. Ordem importa: é o índice do formato YOLO.
    classes: list[str | ClasseNova] = Field(min_length=1)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not SLUG_RE.match(v):
            raise ValueError("slug deve ser minúsculo, alfanumérico, "
                             "separado por hífen (ex: linha-de-inspecao)")
        return v

    @field_validator("classes")
    @classmethod
    def _classes(cls, v: list[str | ClasseNova]) -> list[str | ClasseNova]:
        return _nomes_unicos(v, "classes duplicadas")


class ProjectPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class SchemaAppendIn(BaseModel):
    # Só os nomes a ACRESCENTAR no fim — não a lista inteira. Assim a operação é
    # sempre append por construção: não há como renomear, remover ou reordenar
    # por aqui, que são as mudanças que quebram a compatibilidade.
    add: list[str | ClasseNova] = Field(min_length=1)
    change_note: str | None = Field(default=None, max_length=200)

    @field_validator("add")
    @classmethod
    def _add(cls, v: list[str | ClasseNova]) -> list[str | ClasseNova]:
        return _nomes_unicos(v, "classes duplicadas na lista")


class ProjetoNaLista(BaseModel):
    """`role` é o MEU papel neste projeto — a lista já vem escopada por ele."""
    slug: str
    name: str
    task_type: str
    description: str | None
    created_at: datetime
    role: str


class ProjetoCriado(BaseModel):
    id: UUID
    slug: str
    name: str
    task_type: str
    description: str | None
    created_at: datetime
    role: str
    classes: list[str]


class ClasseDoSchema(BaseModel):
    """Ordem importa: `id` é o índice do formato YOLO. `inside` é o `id` da
    classe principal que recorta esta (só em segmentação); nulo é principal."""
    id: int
    name: str
    inside: int | None = None


class SchemaAtual(BaseModel):
    version: int
    classes: list[ClasseDoSchema]


class SchemaNoHistorico(BaseModel):
    version: int
    back_compatible: bool
    change_note: str | None
    created_at: datetime
    n_classes: int


class ContagensDoProjeto(BaseModel):
    samples: int
    annotations: int
    datasets: int
    models: int


class DetalheDoProjeto(BaseModel):
    id: UUID
    slug: str
    name: str
    task_type: str
    description: str | None
    created_at: datetime
    archived_at: datetime | None
    role: str
    # `schema` colide com um atributo de `BaseModel`; o alias mantém a chave
    # que o `app.js` já lê.
    esquema: SchemaAtual | None = Field(alias="schema")
    schemas: list[SchemaNoHistorico]
    counts: ContagensDoProjeto


class ProjetoAlterado(BaseModel):
    id: UUID
    slug: str
    name: str
    task_type: str
    description: str | None


class SchemaNovo(BaseModel):
    version: int
    classes: list[str]


@router.get("/projects", response_model=list[ProjetoNaLista])
async def list_projects(user: CurrentUser):
    """Projetos que eu enxergo, com meu papel em cada um."""
    return await svc.listar(user)


@router.post("/projects", response_model=ProjetoCriado,
             status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_superuser)])
async def create_project(body: ProjectCreateIn, user: CurrentUser,
                         request: Request):
    """Criar projeto exige superusuário.

    Sem isso, qualquer usuário autenticado criava um projeto, virava `owner`
    dele e com isso ganhava `engineer` — que é justamente o papel exigido para
    navegar buckets e importar. Todo o controle por papel era auto-concedível
    em uma requisição.
    """
    return await svc.criar(user, slug=body.slug, name=body.name,
                           task_type=body.task_type,
                           description=body.description,
                           classes=[como_dict(c) for c in body.classes],
                           ip=auth.client_ip(request))


@router.get("/projects/{project_slug}", response_model=DetalheDoProjeto,
            dependencies=[Depends(require_role("viewer"))])
async def get_project(project_slug: Annotated[str, Path()], user: CurrentUser):
    return await svc.detalhe(project_slug, user)


@router.patch("/projects/{project_slug}", response_model=ProjetoAlterado)
async def patch_project(body: ProjectPatchIn,
                        project_slug: Annotated[str, Path()], request: Request,
                        actor: Annotated[Principal, Depends(require_role("admin"))]):
    return await svc.alterar(project_slug, actor,
                             campos=body.model_dump(exclude_unset=True),
                             ip=auth.client_ip(request))


@router.post("/projects/{project_slug}/schema", response_model=SchemaNovo,
             status_code=status.HTTP_201_CREATED)
async def append_classes(
        body: SchemaAppendIn, project_slug: Annotated[str, Path()],
        request: Request,
        actor: Annotated[Principal, Depends(require_role("engineer"))]):
    """Acrescenta classe(s) no fim → nova versão de schema, back-compatible.

    O schema é imutável (não há UPDATE): toda mudança é uma versão nova. Como só
    acrescenta no fim, os índices antigos não mudam e nada do que já foi rotulado
    é invalidado. Renomear/remover/reordenar não passa por aqui: quebraria a
    compatibilidade e é operação para um caminho próprio.
    """
    return await svc.acrescentar_classes(project_slug, actor,
                                         add=[como_dict(c) for c in body.add],
                                         change_note=body.change_note,
                                         ip=auth.client_ip(request))


@router.post("/projects/{project_slug}/archive",
             status_code=status.HTTP_204_NO_CONTENT)
async def archive_project(project_slug: Annotated[str, Path()],
                          request: Request,
                          actor: Annotated[Principal, Depends(require_role("owner"))]):
    """Arquiva (some das listagens). Não deleta: os dados seguem referenciados
    por datasets, runs e modelos, e apagá-los quebraria a linhagem."""
    await svc.arquivar(project_slug, actor, ip=auth.client_ip(request))
