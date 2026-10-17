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


async def test_admin_nao_remove_owner(admin, client, superuser):
    """A guarda de rebaixar owner não pode ser contornável pela porta do lado.

    `definir_membro` recusa que um admin rebaixe um owner. Sem a mesma guarda
    em `remover_membro`, o admin chega ao mesmo estado com DELETE + PUT: remove
    o owner e o recadastra como viewer. Com vários owners dá para fazer isso
    até sobrar um, sem que nenhum owner precise concordar.
    """
    await admin.post("/v1/projects", json=PROJ)
    async with novo_usuario(admin, client, "co-dono@teste.com") as (_, co):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "co-dono@teste.com", "role": "owner"})
        async with novo_usuario(admin, client, "gerente@teste.com") as (ger, _):
            await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                            json={"email": "gerente@teste.com",
                                  "role": "admin"})
            # não é órfão: ainda haveria um owner — o que barra é o papel
            r = await ger.delete(f"/v1/projects/{PROJ['slug']}/members/{co}")
            assert r.status_code == 403, r.text
        assert await _owners(PROJ["slug"]) == 2


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
    assert d["schema"]["classes"][2] == {"id": 2, "name": "classe_c",
                                         "inside": None}
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


async def test_senha_de_oito_caracteres_basta(admin):
    """O mínimo é 8, não 12 — e 7 continua recusado."""
    r = await admin.post("/v1/users", json={
        "email": "oito@teste.com", "display_name": "oito",
        "password": "12345678"})
    assert r.status_code == 201, r.text
    r = await admin.post("/v1/users", json={
        "email": "sete@teste.com", "display_name": "sete",
        "password": "1234567"})
    assert r.status_code == 422


async def test_candidatos_a_membro(admin, client, superuser):
    """O admin do projeto escolhe de uma lista, e não digita um email às cegas.

    Só entram contas que ainda não são membros e não estão desativadas; e só
    quem administra o projeto vê a lista.
    """
    await admin.post("/v1/projects", json=PROJ)
    base = f"/v1/projects/{PROJ['slug']}"
    async with novo_usuario(admin, client, "ana@teste.com") as (ana, _), \
            novo_usuario(admin, client, "caio@teste.com") as (_, _), \
            novo_usuario(admin, client, "dora@teste.com") as (_, dora_id):
        await admin.post(f"/v1/users/{dora_id}/status",
                         params={"status_": "disabled"})
        await admin.put(f"{base}/members",
                        json={"email": "ana@teste.com", "role": "annotator"})

        r = await admin.get(f"{base}/member-candidates")
        assert r.status_code == 200, r.text
        assert [c["email"] for c in r.json()] == ["caio@teste.com"]
        assert set(r.json()[0]) == {"id", "email", "display_name"}

        assert (await ana.get(f"{base}/member-candidates")).status_code == 403


# --------------------------------------------------------------------------- #
# Segmentação: "Dentro de" — a classe recorte aponta para a principal
# --------------------------------------------------------------------------- #

SEG = {"slug": "carcaca", "name": "Carcaça", "task_type": "segment",
       "classes": ["banda"]}


async def test_segmentacao_cria_recorte_dentro_da_principal(admin):
    r = await admin.post("/v1/projects", json={
        **SEG, "classes": ["banda", {"name": "hematoma", "inside": "banda"}]})
    assert r.status_code == 201, r.text
    assert r.json()["classes"] == ["banda", "hematoma"]

    d = (await admin.get(f"/v1/projects/{SEG['slug']}")).json()
    assert d["schema"]["classes"] == [
        {"id": 0, "name": "banda", "inside": None},
        {"id": 1, "name": "hematoma", "inside": 0}]


async def test_append_recorte_aponta_para_principal_existente(admin):
    await admin.post("/v1/projects", json=SEG)
    r = await admin.post(f"/v1/projects/{SEG['slug']}/schema", json={
        "add": ["picanha", {"name": "fraldinha", "inside": "banda"}]})
    assert r.status_code == 201, r.text
    d = (await admin.get(f"/v1/projects/{SEG['slug']}")).json()
    # `picanha` sem "Dentro de" é principal: o campo é opcional mesmo em
    # segmentação.
    assert d["schema"]["classes"][1:] == [
        {"id": 1, "name": "picanha", "inside": None},
        {"id": 2, "name": "fraldinha", "inside": 0}]


@pytest.mark.parametrize("classes,trecho", [
    # pai que não existe
    (["banda", {"name": "hematoma", "inside": "carcaca"}], "não existe"),
    # recorte dentro de recorte: só um nível
    (["banda", {"name": "a", "inside": "banda"}, {"name": "b", "inside": "a"}],
     "principal"),
    # pai declarado depois do filho: o índice do pai seria maior que o dele
    ([{"name": "hematoma", "inside": "banda"}, "banda"], "não existe"),
    # dentro de si mesma
    ([{"name": "banda", "inside": "banda"}], "não existe"),
])
async def test_dentro_de_invalido_recusa(admin, classes, trecho):
    r = await admin.post("/v1/projects", json={**SEG, "classes": classes})
    assert r.status_code == 422, r.text
    assert trecho in r.json()["detail"]


async def test_dentro_de_so_em_projeto_de_segmentacao(admin):
    r = await admin.post("/v1/projects", json={
        **PROJ, "classes": ["a", {"name": "b", "inside": "a"}]})
    assert r.status_code == 422
    assert "segmentação" in r.json()["detail"]

    await admin.post("/v1/projects", json=PROJ)
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/schema", json={
        "add": [{"name": "c", "inside": "classe_a"}]})
    assert r.status_code == 422
    assert "segmentação" in r.json()["detail"]


async def test_append_recorte_dentro_de_recorte_recusa(admin):
    await admin.post("/v1/projects", json={
        **SEG, "classes": ["banda", {"name": "hematoma", "inside": "banda"}]})
    r = await admin.post(f"/v1/projects/{SEG['slug']}/schema", json={
        "add": [{"name": "x", "inside": "hematoma"}]})
    assert r.status_code == 422
    assert "principal" in r.json()["detail"]


async def test_dentro_de_duplicado_por_nome_recusa(admin):
    r = await admin.post("/v1/projects", json={
        **SEG, "classes": ["banda", {"name": "banda", "inside": None}]})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Gabarito de cortes: as linhas e as sementes que o anotador aplica numa
# principal nova
# --------------------------------------------------------------------------- #

CORTES = {**SEG, "classes": ["banda", {"name": "dianteiro", "inside": "banda"},
                             {"name": "traseiro", "inside": "banda"},
                             "rabo", {"name": "ponta", "inside": "rabo"}]}
GABARITO = {"lines": [[[-0.1, 0.45], [0.5, 0.5], [1.1, 0.48]]],
            "seeds": [{"pt": [0.5, 0.2], "cls": 2}, {"pt": [0.5, 0.8], "cls": 1}],
            "shapes": []}
# Só laços (picanha, maminha): regiões dentro da banda, sem corte nenhum
LACOS = {"lines": [], "seeds": [],
         "shapes": [{"cls": 1, "pts": [[0.2, 0.2], [0.4, 0.2], [0.3, 0.4]]}]}


async def test_gabarito_grava_le_e_substitui(admin):
    await admin.post("/v1/projects", json=CORTES)
    assert (await admin.get(f"/v1/projects/{SEG['slug']}/templates")).json() == []

    r = await admin.put(f"/v1/projects/{SEG['slug']}/templates/0", json=GABARITO)
    assert r.status_code == 200, r.text
    assert r.json()["class_index"] == 0
    assert r.json()["lines"] == GABARITO["lines"]
    assert r.json()["seeds"] == GABARITO["seeds"]

    # um por principal: gravar de novo substitui
    outro = {**GABARITO, "lines": [[[0, 0.3], [1, 0.3]]]}
    assert (await admin.put(f"/v1/projects/{SEG['slug']}/templates/0",
                            json=outro)).status_code == 200
    lista = (await admin.get(f"/v1/projects/{SEG['slug']}/templates")).json()
    assert [g["class_index"] for g in lista] == [0]
    assert lista[0]["lines"] == outro["lines"]


async def test_gabarito_so_em_segmentacao(admin):
    await admin.post("/v1/projects", json=PROJ)
    r = await admin.put(f"/v1/projects/{PROJ['slug']}/templates/0",
                        json={"lines": [[[0, 0], [1, 1]]], "seeds": []})
    assert r.status_code == 422
    assert "segmentação" in r.json()["detail"]


@pytest.mark.parametrize("indice,trecho", [
    (1, "principal"),          # dianteiro é recorte
    (9, "não existe"),
])
async def test_gabarito_so_de_classe_principal(admin, indice, trecho):
    await admin.post("/v1/projects", json=CORTES)
    r = await admin.put(f"/v1/projects/{SEG['slug']}/templates/{indice}",
                        json=GABARITO)
    assert r.status_code == 422
    assert trecho in r.json()["detail"]


@pytest.mark.parametrize("cls", [0, 4])    # a própria banda; ponta é do rabo
async def test_semente_so_de_recorte_da_principal(admin, cls):
    """Semente com classe de fora daria ao pedaço da banda uma classe que não
    está "dentro de" banda — o anotador a gravaria assim."""
    await admin.post("/v1/projects", json=CORTES)
    r = await admin.put(f"/v1/projects/{SEG['slug']}/templates/0", json={
        **GABARITO, "seeds": [{"pt": [0.5, 0.5], "cls": cls}]})
    assert r.status_code == 422
    assert "dentro de" in r.json()["detail"]


async def test_gabarito_so_de_lacos(admin):
    await admin.post("/v1/projects", json=CORTES)
    r = await admin.put(f"/v1/projects/{SEG['slug']}/templates/0", json=LACOS)
    assert r.status_code == 200, r.text
    assert r.json()["shapes"] == LACOS["shapes"]
    assert r.json()["lines"] == []


@pytest.mark.parametrize("corpo,trecho", [
    ({"lines": [], "seeds": [], "shapes": []}, "vazio"),
    # laço com classe de fora da banda (ponta é do rabo)
    ({**LACOS, "shapes": [{"cls": 4, "pts": [[0.2, 0.2], [0.4, 0.2], [0.3, 0.4]]}]},
     "dentro de"),
])
async def test_gabarito_vazio_ou_laco_de_fora_recusa(admin, corpo, trecho):
    await admin.post("/v1/projects", json=CORTES)
    r = await admin.put(f"/v1/projects/{SEG['slug']}/templates/0", json=corpo)
    assert r.status_code == 422, r.text
    assert trecho in r.text


@pytest.mark.parametrize("lines", [
    [[[0.1, 0.1]]],                       # linha de um ponto
    [[[0.1, 0.1], [50, 0.2]]],            # coordenada absurda
])
async def test_gabarito_linhas_invalidas(admin, lines):
    await admin.post("/v1/projects", json=CORTES)
    r = await admin.put(f"/v1/projects/{SEG['slug']}/templates/0",
                        json={**GABARITO, "lines": lines})
    assert r.status_code == 422


@pytest.mark.parametrize("pt", [["NaN", 0.5], [0.5, 50]])
async def test_semente_fora_do_quadro_recusa(admin, pt):
    """NaN passava pelo pydantic e estourava no cast para jsonb: 500."""
    await admin.post("/v1/projects", json=CORTES)
    r = await admin.put(f"/v1/projects/{SEG['slug']}/templates/0", json={
        **GABARITO, "seeds": [{"pt": pt, "cls": 1}]})
    assert r.status_code == 422, r.text


async def test_gabarito_annotator_grava_viewer_nao(admin, client):
    await admin.post("/v1/projects", json=CORTES)
    async with novo_usuario(admin, client, "vera@teste.com") as (vera, _):
        await admin.put(f"/v1/projects/{SEG['slug']}/members",
                        json={"email": "vera@teste.com", "role": "viewer"})
        r = await vera.get(f"/v1/projects/{SEG['slug']}/templates")
        assert r.status_code == 200
        r = await vera.put(f"/v1/projects/{SEG['slug']}/templates/0", json=GABARITO)
        assert r.status_code == 403

        await admin.put(f"/v1/projects/{SEG['slug']}/members",
                        json={"email": "vera@teste.com", "role": "annotator"})
        r = await vera.put(f"/v1/projects/{SEG['slug']}/templates/0", json=GABARITO)
        assert r.status_code == 200, r.text
