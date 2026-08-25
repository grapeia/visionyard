"""Projetos, papéis e o que cada papel pode fazer."""
from __future__ import annotations

from contextlib import asynccontextmanager

import psycopg
import pytest

from .conftest import TEST_DSN

pytestmark = pytest.mark.anyio

PROJ = {"slug": "linha-de-inspecao", "name": "Linha de inspeção", "task_type": "detect",
        "classes": ["classe_a", "classe_b"]}


@asynccontextmanager
async def novo_usuario(admin, client, email, password="senha-de-teste-123"):
    """Cria usuário ativo e cede um cliente já logado como ele."""
    r = await admin.post("/v1/users", json={
        "email": email, "display_name": email.split("@")[0],
        "password": password})
    assert r.status_code == 201, r.text
    async with client.__class__(transport=client._transport,
                                base_url="http://test") as c:
        await c.post("/v1/login", json={"email": email, "password": password})
        yield c, r.json()["id"]


async def test_criar_projeto_cria_schema_e_owner(admin):
    r = await admin.post("/v1/projects", json=PROJ)
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "owner"
    assert r.json()["classes"] == ["classe_a", "classe_b"]

    r = await admin.get(f"/v1/projects/{PROJ['slug']}")
    assert r.status_code == 200
    assert r.json()["schema"]["version"] == 1
    assert [c["name"] for c in r.json()["schema"]["classes"]] == PROJ["classes"]
    assert r.json()["counts"]["samples"] == 0


async def test_slug_invalido_rejeitado(admin):
    r = await admin.post("/v1/projects", json={**PROJ, "slug": "Linha A"})
    assert r.status_code == 422


async def test_slug_duplicado(admin):
    assert (await admin.post("/v1/projects", json=PROJ)).status_code == 201
    assert (await admin.post("/v1/projects", json=PROJ)).status_code == 409


async def test_projeto_sem_classes_rejeitado(admin):
    r = await admin.post("/v1/projects", json={**PROJ, "classes": []})
    assert r.status_code == 422


async def test_nao_membro_recebe_404_e_nao_403(admin, client):
    """404 e não 403: 403 revelaria que o projeto existe."""
    await admin.post("/v1/projects", json=PROJ)
    async with novo_usuario(admin, client, "estranho@teste.com") as (outro, _):
        r = await outro.get(f"/v1/projects/{PROJ['slug']}")
        assert r.status_code == 404
        assert (await outro.get("/v1/projects")).json() == []


async def test_annotator_nao_administra_membros(admin, client):
    await admin.post("/v1/projects", json=PROJ)
    async with novo_usuario(admin, client, "ana@teste.com") as (ana, _):
        r = await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                            json={"email": "ana@teste.com", "role": "annotator"})
        assert r.status_code == 200

        # enxerga o projeto...
        assert (await ana.get(f"/v1/projects/{PROJ['slug']}")).status_code == 200
        # ...mas não promove ninguém
        r = await ana.put(f"/v1/projects/{PROJ['slug']}/members",
                          json={"email": "ana@teste.com", "role": "owner"})
        assert r.status_code == 403


async def test_admin_nao_concede_owner(admin, client):
    """Senão um admin se promoveria sozinho a owner."""
    await admin.post("/v1/projects", json=PROJ)
    async with novo_usuario(admin, client, "bruno@teste.com") as (bruno, _):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "bruno@teste.com", "role": "admin"})
        r = await bruno.put(f"/v1/projects/{PROJ['slug']}/members",
                            json={"email": "bruno@teste.com", "role": "owner"})
        assert r.status_code == 403


async def test_nao_deixa_projeto_sem_owner(admin, superuser):
    r = await admin.post("/v1/projects", json=PROJ)
    r = await admin.delete(
        f"/v1/projects/{PROJ['slug']}/members/{superuser['id']}")
    assert r.status_code == 409
    assert "sem owner" in r.json()["detail"]


async def _owners(project_slug: str) -> int:
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        return conn.execute(
            "SELECT count(*) FROM project_member m JOIN project p"
            "  ON p.id = m.project_id"
            " WHERE p.slug = %s AND m.role = 'owner'",
            (project_slug,)).fetchone()[0]


async def test_dois_owners_saindo_ao_mesmo_tempo_nao_esvaziam_o_projeto(
        admin, client, superuser):
    """A guarda "o projeto ficaria sem owner" contava numa conexão à parte.

    Duas remoções simultâneas liam 2 antes de qualquer uma gravar, as duas
    passavam pela guarda e o projeto ficava órfão: sem owner, ninguém mais
    arquiva nem concede owner, e o papel máximo do projeto vira `admin`. Contar
    dentro da transação não bastaria — em READ COMMITTED cada comando pega
    snapshot novo, e as duas continuariam vendo 2. O que serializa é o lock
    consultivo por projeto, como no append de schema e no congelamento.
    """
    import asyncio

    await admin.post("/v1/projects", json=PROJ)
    async with novo_usuario(admin, client, "co-dono@teste.com") as (_, outro):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "co-dono@teste.com", "role": "owner"})
        assert await _owners(PROJ["slug"]) == 2

        base = f"/v1/projects/{PROJ['slug']}/members"
        respostas = await asyncio.gather(
            admin.delete(f"{base}/{superuser['id']}"),
            admin.delete(f"{base}/{outro}"))

    codigos = sorted(r.status_code for r in respostas)
    assert codigos == [204, 409], codigos
    assert await _owners(PROJ["slug"]) == 1


async def test_usuario_comum_nao_cria_projeto(admin, client):
    """Criar projeto torna o criador owner, e owner implica engineer.

    Se qualquer autenticado pudesse criar projeto, todo controle por papel
    seria auto-concedível: bastaria criar um projeto para ganhar permissão de
    navegar bucket e importar.
    """
    async with novo_usuario(admin, client, "dono@teste.com") as (dono, _):
        r = await dono.post("/v1/projects", json=PROJ)
        assert r.status_code == 403


async def test_superuser_enxerga_projeto_alheio(admin, client, superuser):
    """Superusuário passa por cima do RBAC de projeto — é o papel dele."""
    await admin.post("/v1/projects", json=PROJ)
    await admin.delete(
        f"/v1/projects/{PROJ['slug']}/members/{superuser['id']}")
    r = await admin.get(f"/v1/projects/{PROJ['slug']}")
    assert r.status_code == 200
    assert r.json()["role"] == "owner"


async def test_arquivar_some_da_listagem(admin):
    await admin.post("/v1/projects", json=PROJ)
    assert len((await admin.get("/v1/projects")).json()) == 1
    assert (await admin.post(
        f"/v1/projects/{PROJ['slug']}/archive")).status_code == 204
    assert (await admin.get("/v1/projects")).json() == []
    assert (await admin.post(
        f"/v1/projects/{PROJ['slug']}/archive")).status_code == 409


async def test_patch_registra_auditoria(admin):
    await admin.post("/v1/projects", json=PROJ)
    r = await admin.patch(f"/v1/projects/{PROJ['slug']}",
                          json={"name": "Linha de inspeção — A"})
    assert r.status_code == 200
    assert r.json()["name"] == "Linha de inspeção — A"

    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        row = conn.execute(
            "SELECT action, before, after, actor_label FROM audit_log"
            " WHERE action = 'project.update'").fetchone()
    assert row is not None
    assert row[1]["name"] == "Linha de inspeção"
    assert row[2]["name"] == "Linha de inspeção — A"


async def test_usuario_comum_nao_lista_usuarios(admin, client):
    async with novo_usuario(admin, client, "comum@teste.com") as (comum, _):
        assert (await comum.get("/v1/users")).status_code == 403
        assert (await comum.get("/v1/me")).status_code == 200


async def test_admin_nao_rebaixa_ultimo_owner(admin, client, superuser):
    """A guarda existia em remove_member e faltava no upsert.

    Sem ela, um admin rebaixava o único owner para viewer e o projeto ficava
    órfão: ninguém mais podia arquivá-lo nem conceder owner.
    """
    await admin.post("/v1/projects", json=PROJ)
    async with novo_usuario(admin, client, "bruno@teste.com") as (bruno, _):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "bruno@teste.com", "role": "admin"})
        r = await bruno.put(f"/v1/projects/{PROJ['slug']}/members",
                            json={"email": superuser["email"], "role": "viewer"})
        assert r.status_code in (403, 409)

    r = await admin.get(f"/v1/projects/{PROJ['slug']}/members")
    papeis = {m["email"]: m["role"] for m in r.json()}
    assert papeis[superuser["email"]] == "owner"


async def test_projeto_arquivado_e_somente_leitura(admin):
    """Arquivar sumia da listagem mas não impedia nada."""
    await admin.post("/v1/projects", json=PROJ)
    await admin.post(f"/v1/projects/{PROJ['slug']}/archive")

    assert (await admin.get(f"/v1/projects/{PROJ['slug']}")).status_code == 200
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/datasets",
                         json={"slug": "x", "name": "X", "kind": "train"})
    assert r.status_code == 409
    assert "arquivado" in r.json()["detail"]


async def test_trocar_senha_revoga_token_de_api(admin, superuser, client):
    """Quem troca a senha porque ela vazou espera cortar TODO acesso.

    Revogar só a sessão deixava o atacante dentro: um token criado por ele
    segue autenticando via Bearer e, sem expires_at, nunca expira.
    """
    r = await admin.post("/v1/me/tokens", json={"name": "do-atacante"})
    token = r.json()["token"]

    async with client.__class__(transport=client._transport,
                                base_url="http://test") as c2:
        h = {"Authorization": f"Bearer {token}"}
        assert (await c2.get("/v1/me", headers=h)).status_code == 200

        r = await admin.post("/v1/me/password", json={
            "current_password": superuser["password"],
            "new_password": "outra-senha-bem-longa"})
        assert r.status_code == 204

        assert (await c2.get("/v1/me", headers=h)).status_code == 401


# --------------------------------------------------------------------------- #
# Adicionar classe: nova versão de schema, back-compatible
# --------------------------------------------------------------------------- #

async def test_append_classe_gera_versao_back_compatible(admin):
    await admin.post("/v1/projects", json=PROJ)
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/schema",
                         json={"add": ["classe_c"]})
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 2
    assert r.json()["classes"] == ["classe_a", "classe_b", "classe_c"]

    d = (await admin.get(f"/v1/projects/{PROJ['slug']}")).json()
    assert d["schema"]["version"] == 2
    # índices antigos preservados; a nova classe é o índice 2
    assert d["schema"]["classes"][2] == {"id": 2, "name": "classe_c"}
    assert d["schemas"][0]["back_compatible"] is True
    assert len(d["schemas"]) == 2


async def test_append_nao_invalida_anotacao_antiga(admin, tmp_path):
    """Uma anotação em cls=1 feita na v1 continua válida na v2 (append)."""
    import tests.test_imports as ti

    from .test_datasets import PROJ as DPROJ
    from .test_datasets import _projeto_com_dados
    ti.PROJ = DPROJ
    await _projeto_com_dados(admin, tmp_path)   # cria projeto com 5 anotações
    antes = (await admin.get(f"/v1/projects/{DPROJ['slug']}")).json()
    assert antes["counts"]["annotations"] == 5

    r = await admin.post(f"/v1/projects/{DPROJ['slug']}/schema",
                         json={"add": ["nova"]})
    assert r.status_code == 201

    # o dataset ainda resolve as 5 anotações (schemas compatíveis contam a v1)
    from .test_datasets import DS, _source_id
    await admin.post(f"/v1/projects/{DPROJ['slug']}/datasets", json=DS)
    src = await _source_id(DPROJ["slug"])
    base = f"/v1/projects/{DPROJ['slug']}/datasets/{DS['slug']}"
    await admin.put(f"{base}/sources/{src}")
    p = (await admin.get(f"{base}/preview")).json()
    assert p["anotadas"] == 5, "append quebrou a compatibilidade das anotações"


async def test_append_classe_duplicada_recusa(admin):
    await admin.post("/v1/projects", json=PROJ)
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/schema",
                         json={"add": ["classe_a"]})
    assert r.status_code == 409
    assert "já existe" in r.json()["detail"]


async def test_append_exige_engineer(admin, client):
    await admin.post("/v1/projects", json=PROJ)
    async with novo_usuario(admin, client, "rot@teste.com") as (rot, _):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "rot@teste.com", "role": "annotator"})
        r = await rot.post(f"/v1/projects/{PROJ['slug']}/schema",
                           json={"add": ["x"]})
        assert r.status_code == 403


async def test_append_superuser_slug_invalido_404(admin):
    r = await admin.post("/v1/projects/nao-existe/schema", json={"add": ["x"]})
    assert r.status_code == 404


@pytest.mark.parametrize("metodo,rota,corpo", [
    ("GET",    "", None),
    ("PATCH",  "", {"name": "outro nome"}),
    ("GET",    "/samples", None),
    ("POST",   "/datasets", {"slug": "ds", "name": "D", "kind": "train"}),
    ("PUT",    "/members", {"email": "admin@teste.com", "role": "viewer"}),
    ("DELETE", "/members/00000000-0000-0000-0000-000000000000", None),
    ("POST",   "/archive", None),
])
async def test_superuser_em_projeto_inexistente_recebe_404(admin, metodo, rota,
                                                           corpo):
    """`require_role` deixa o superusuário passar sem conferir se o projeto
    existe (`project_role` devolve 'owner' de bandeja). O handler indexava o
    `SELECT ... FROM project` direto e o slug errado virava 500."""
    r = await admin.request(metodo, f"/v1/projects/nao-existe{rota}", json=corpo)
    assert r.status_code == 404, f"{metodo} {rota} → {r.status_code}: {r.text}"
