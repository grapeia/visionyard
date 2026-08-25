"""A disciplina de conexão dos serviços já migrados (item 15 do PLANO).

Duas regras, as duas varridas por AST porque nenhuma das duas cabe no tipo:

1. **Nada alcança o pool sozinho.** `db.fetch_*`/`db.execute` sem `conn=` pegam
   uma conexão cada, e num módulo que faz leia-decida-escreva isso escapa da
   transação sem que nada acuse — o tipo aceita, o lint aceita e a suíte passa,
   porque o resultado só diverge sob concorrência. `db.py` precisa manter o
   `conn=None` opcional enquanto houver serviço não migrado, então o `mypy` não
   tem como cobrar; a regra vale por módulo, conforme cada um é convertido.

2. **`conn.transaction()` vai no mesmo `async with` que abre a conexão.** O
   psycopg decide o que o bloco é pelo estado da conexão: ociosa, abre
   transação; já `INTRANS` — e uma leitura basta —, abre um `SAVEPOINT`. No
   segundo caso o código não muda de aparência, mas o commit passa a acontecer
   quando o pool devolve a conexão, e as leituras de antes do bloco entram na
   mesma transação que as escritas de dentro. A fronteira que a linha aparenta
   marcar deixa de existir.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Todo módulo que fala com o banco, e não só os de `services/`: `auth.py` é o
# serviço do domínio de autenticação e `deps.py` resolve papel e projeto, os
# dois com SQL próprio. Fora da lista fica `services/buckets.py`, por não ter
# uma linha de SQL — o que ele guarda é a cerca da allowlist, e o acesso ao GCS
# passa por `storage.py`. Módulo novo com SQL entra aqui.
MODULOS = ["auth.py", "deps.py",
           "services/agents.py", "services/datasets.py",
           "services/devices.py", "services/imports.py", "services/jobs.py",
           "services/models.py", "services/prelabel.py",
           "services/projects.py", "services/samples.py",
           "services/training.py", "services/users.py"]

# Acessos de `db.py`: só valem como `db.X(..., conn=...)`.
ACESSOS_DB = {"fetch_one", "fetch_all", "fetch_row", "execute"}

# `db.py` é o próprio dono dos acessos: varrê-lo se acusaria a si mesmo.

# Auxiliares de fora de `db.py` que também alcançam o pool e aceitam `conn=`.
ACESSOS_VIZINHOS = {"project_role"}

# Auxiliares de `deps` que alcançam o pool e NÃO aceitam `conn=`: importá-los é
# o vazamento. O nome é conferido junto com o módulo — `services/projects.py`
# exporta uma `projeto_ou_404` própria, essa com `conn`, e é dela que os outros
# serviços se servem.
SEM_CONEXAO = {"projeto_ou_404"}


def _arvore(modulo: str) -> ast.Module:
    caminho = Path(__file__).resolve().parents[1] / "api" / modulo
    return ast.parse(caminho.read_text())


def _definidas(arvore: ast.Module) -> set[str]:
    """Funções que o próprio módulo define.

    Quem é dono do acesso não é cobrado por ele: `deps.py` define
    `project_role`, e é lá que ela é chamada sem `conn` — pela dependência do
    FastAPI, que é justamente quem não tem conexão nenhuma na mão.
    """
    return {no.name for no in ast.walk(arvore)
            if isinstance(no, ast.AsyncFunctionDef | ast.FunctionDef)}


def _nome_chamado(no: ast.Call, proprias: set[str]) -> str | None:
    """`db.fetch_one(...)`, `fetch_one(...)`, `project_role(...)`.

    `conn.execute(...)` fica de fora: ali a conexão é o próprio objeto chamado,
    que é justamente o que se quer. O que se procura é o acesso que vai buscar
    conexão sozinho.
    """
    if (isinstance(no.func, ast.Attribute) and no.func.attr in ACESSOS_DB
            and isinstance(no.func.value, ast.Name)
            and no.func.value.id == "db"):
        return f"db.{no.func.attr}"
    if (isinstance(no.func, ast.Name)
            and no.func.id in (ACESSOS_DB | ACESSOS_VIZINHOS) - proprias):
        return no.func.id
    return None


@pytest.mark.parametrize("modulo", MODULOS)
def test_modulo_nunca_alcanca_o_pool_sozinho(modulo: str):
    arvore = _arvore(modulo)
    proprias = _definidas(arvore)
    soltas = [f"linha {no.lineno}: {nome}(...)"
              for no in ast.walk(arvore)
              if isinstance(no, ast.Call)
              and (nome := _nome_chamado(no, proprias))
              and not any(k.arg == "conn" for k in no.keywords)]
    assert not soltas, ("sem conn=, cada uma pega a sua conexão: "
                        + "; ".join(soltas))

    importados = {(a.name, a.asname) for no in ast.walk(arvore)
                  if isinstance(no, ast.ImportFrom) for a in no.names}
    nomes = {n for n, _ in importados}
    vazando = {a.name for no in ast.walk(arvore)
               if isinstance(no, ast.ImportFrom) and no.module == "deps"
               for a in no.names if a.name in SEM_CONEXAO}
    assert not vazando, (
        f"`deps.{sorted(vazando)[0]}` alcança o pool por conta própria — use a "
        f"versão com `conn` de `services/`")
    # As duas formas que fariam a varredura de cima olhar para o lado:
    # importar o acesso solto, ou apelidar o módulo.
    assert not (ACESSOS_DB & nomes), (
        f"importe o módulo `db`, não {sorted(ACESSOS_DB & nomes)} — solto, o "
        f"acesso deixa de ser reconhecível na varredura")
    assert ("db", None) in importados, "o módulo `db` tem de entrar sem apelido"


@pytest.mark.parametrize("modulo", MODULOS)
def test_transacao_do_servico_nunca_e_savepoint(modulo: str):
    def abre(item: ast.withitem, nome: str) -> bool:
        c = item.context_expr
        return (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == nome)

    soltas = [f"linha {no.lineno}"
              for no in ast.walk(_arvore(modulo))
              if isinstance(no, ast.AsyncWith)
              and any(abre(i, "transaction") for i in no.items)
              and not any(abre(i, "connection") for i in no.items)]
    assert not soltas, ("transaction() longe do connection() vira savepoint: "
                        + "; ".join(soltas))
