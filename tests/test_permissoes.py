"""Permissões por papel editáveis pelo superusuário.

O que importa conferir: sem nada gravado vale exatamente o que as rotas
exigiam antes (a migração não muda o comportamento de ninguém); a exceção
gravada vale na próxima requisição, para conceder e para negar; owner não é
configurável; e quem recebe a gestão de membros não se promove acima do
próprio papel.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import psycopg
import pytest

from api.services import permissions

from .conftest import TEST_DSN
from .test_datasets import DS, PROJ
from .test_projects import novo_usuario

pytestmark = pytest.mark.anyio

SLUG = PROJ["slug"]
ROUTERS = Path(__file__).resolve().parents[1] / "api" / "routers"


async def _membro(admin, email: str, role: str) -> None:
    r = await admin.put(f"/v1/projects/{SLUG}/members",
                        json={"email": email, "role": role})
    assert r.status_code == 200, r.text


async def test_so_superusuario(admin, client):
    await admin.post("/v1/projects", json=PROJ)
    async with novo_usuario(admin, client, "adm@teste.com") as (adm, _):
        await _membro(admin, "adm@teste.com", "admin")
        assert (await adm.get("/v1/permissions")).status_code == 403
        r = await adm.put("/v1/permissions/datasets.lista.criar/viewer",
                          json={"permitido": True})
        assert r.status_code == 403
        r = await adm.delete("/v1/permissions/datasets.lista.criar/viewer")
        assert r.status_code == 403


async def test_sem_excecao_vale_o_papel_minimo_de_antes(admin):
    m = (await admin.get("/v1/permissions")).json()
    assert [p["chave"] for p in m["papeis"]] == list(permissions.CONFIGURAVEIS)
    assert [a["chave"] for a in m["acoes"]] == list(permissions.ACOES)
    rank = permissions.ROLE_RANK
    for a in m["acoes"]:
        assert set(a["papeis"]) == set(permissions.CONFIGURAVEIS)
        for papel, c in a["papeis"].items():
            assert c == {"permitido": rank[papel] >= rank[a["padrao"]],
                         "alterado": False}, (a["chave"], papel)


async def test_negar_ao_engenheiro_vale_na_proxima_requisicao(admin, client):
    await admin.post("/v1/projects", json=PROJ)
    async with novo_usuario(admin, client, "eng@teste.com") as (eng, _):
        await _membro(admin, "eng@teste.com", "engineer")
        pode = (await eng.get(f"/v1/projects/{SLUG}")).json()["permissoes"]
        assert "datasets.lista.criar" in pode

        r = await admin.put("/v1/permissions/datasets.lista.criar/engineer",
                            json={"permitido": False})
        assert r.status_code == 200, r.text
        assert r.json()["papeis"]["engineer"] == {"permitido": False,
                                                  "alterado": True}
        # admin continua pelo padrão: a exceção é do papel, não da ação
        assert r.json()["papeis"]["admin"]["permitido"] is True

        r = await eng.post(f"/v1/projects/{SLUG}/datasets", json=DS)
        assert r.status_code == 403
        pode = (await eng.get(f"/v1/projects/{SLUG}")).json()["permissoes"]
        assert "datasets.lista.criar" not in pode

        r = await admin.delete("/v1/permissions/datasets.lista.criar/engineer")
        assert r.json()["papeis"]["engineer"] == {"permitido": True,
                                                  "alterado": False}
        r = await eng.post(f"/v1/projects/{SLUG}/datasets", json=DS)
        assert r.status_code == 201, r.text


async def test_conceder_ao_anotador(admin, client):
    await admin.post("/v1/projects", json=PROJ)
    async with novo_usuario(admin, client, "ano@teste.com") as (ano, _):
        await _membro(admin, "ano@teste.com", "annotator")
        r = await ano.post(f"/v1/projects/{SLUG}/datasets", json=DS)
        assert r.status_code == 403
        await admin.put("/v1/permissions/datasets.lista.criar/annotator",
                        json={"permitido": True})
        r = await ano.post(f"/v1/projects/{SLUG}/datasets", json=DS)
        assert r.status_code == 201, r.text


async def test_igual_ao_padrao_nao_grava_linha(admin):
    """Marcar o que já era o padrão não pode congelar o valor: uma mudança
    futura do padrão no código tem que chegar a esse papel."""
    r = await admin.put("/v1/permissions/datasets.lista.criar/engineer",
                        json={"permitido": True})
    assert r.json()["papeis"]["engineer"]["alterado"] is False
    with psycopg.connect(TEST_DSN) as conn:
        n = conn.execute("SELECT count(*) FROM role_permission").fetchone()
    assert n is not None and n[0] == 0


async def test_mudanca_fica_na_trilha(admin):
    await admin.put("/v1/permissions/modelos.versoes.baixar/viewer",
                    json={"permitido": True})
    # repetir não é mudança, e não vira segunda linha de auditoria
    await admin.put("/v1/permissions/modelos.versoes.baixar/viewer",
                    json={"permitido": True})
    with psycopg.connect(TEST_DSN) as conn:
        rows = conn.execute(
            "SELECT before, after FROM audit_log"
            " WHERE action = 'permission.set'").fetchall()
    assert len(rows) == 1
    assert rows[0][0]["allowed"] is False and rows[0][1]["allowed"] is True


async def test_owner_nao_e_configuravel(admin):
    r = await admin.put("/v1/permissions/projeto.geral.arquivar/owner",
                        json={"permitido": False})
    assert r.status_code == 422
    r = await admin.put("/v1/permissions/projeto.geral.arquivar/chefe",
                        json={"permitido": True})
    assert r.status_code == 422
    r = await admin.put("/v1/permissions/nao.existe.isto/viewer",
                        json={"permitido": True})
    assert r.status_code == 404


async def test_superusuario_em_projeto_inexistente_e_404(admin):
    """`require_role` deixava o superusuário passar sem consultar nada e o
    404 vinha do handler; a decisão por ação lê a linha do projeto."""
    r = await admin.post("/v1/projects/nao-existe/datasets", json=DS)
    assert r.status_code == 404
    # buckets não consulta o projeto no serviço: antes respondia 200 aqui
    assert (await admin.get("/v1/projects/nao-existe/buckets")).status_code == 404


async def test_quem_gere_membros_nao_passa_do_proprio_papel(admin, client):
    await admin.post("/v1/projects", json=PROJ)
    await admin.put("/v1/permissions/projeto.membros.gerenciar/annotator",
                    json={"permitido": True})
    async with novo_usuario(admin, client, "ano@teste.com") as (ano, _), \
            novo_usuario(admin, client, "eng@teste.com") as (_, eng_id), \
            novo_usuario(admin, client, "v@teste.com"):
        await _membro(admin, "ano@teste.com", "annotator")
        await _membro(admin, "eng@teste.com", "engineer")
        url = f"/v1/projects/{SLUG}/members"
        # concede até o próprio papel
        r = await ano.put(url, json={"email": "v@teste.com", "role": "viewer"})
        assert r.status_code == 200, r.text
        # e não acima: senão se promovia
        r = await ano.put(url, json={"email": "ano@teste.com",
                                     "role": "engineer"})
        assert r.status_code == 403
        # nem mexe em quem está acima
        r = await ano.put(url, json={"email": "eng@teste.com",
                                     "role": "viewer"})
        assert r.status_code == 403
        assert (await ano.delete(f"{url}/{eng_id}")).status_code == 403


def _dependencias_de_papel() -> list[tuple[str, int, str]]:
    achadas = []
    for arq in sorted(ROUTERS.glob("*.py")):
        for no in ast.walk(ast.parse(arq.read_text())):
            if (isinstance(no, ast.Call) and isinstance(no.func, ast.Name)
                    and no.func.id in ("require_role", "require_action")
                    and no.args and isinstance(no.args[0], ast.Constant)):
                achadas.append((f"{arq.name}:{no.lineno}", no.func.id,
                                no.args[0].value))
    return achadas


def test_rota_que_escreve_declara_acao():
    """Papel mínimo fixo na rota é o que esta matriz existe para substituir:
    `require_role` só sobra para leitura, onde ser membro é o que basta."""
    fixas = [(onde, papel) for onde, dep, papel in _dependencias_de_papel()
             if dep == "require_role" and papel != "viewer"]
    assert fixas == []


def test_todo_o_catalogo_esta_em_uso():
    """Ação no catálogo que nenhuma rota exige é uma caixa na tela que não
    controla nada."""
    usadas = {v for _, dep, v in _dependencias_de_papel()
              if dep == "require_action"}
    assert usadas == set(permissions.ACOES)
    assert all(re.fullmatch(r"[a-z]+\.[a-z]+\.[a-z]+", k)
               for k in permissions.ACOES)
