"""O primeiro superusuário, pelo `db.seed`.

No Cloud Run o seed roda como job, e tudo o que ele imprime vai para o Cloud
Logging — uma senha gerada e impressa ficaria lá, legível a quem lê log do
projeto. Por isso a senha também pode vir de variável (que o job recebe do
Secret Manager), e aí não é impressa.
"""
from __future__ import annotations

import sys

import psycopg

from api.security import verify_password
from db import seed

from .conftest import TEST_DSN


def _rodar(monkeypatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["db.seed", *args])
    return seed.main()


def _hash(email: str) -> str:
    with psycopg.connect(TEST_DSN) as conn:
        return conn.execute("SELECT password_hash FROM app_user WHERE email = %s",
                            (email,)).fetchone()[0]


def test_senha_da_variavel_nao_e_impressa(monkeypatch, capsys):
    monkeypatch.setenv("VISIONYARD_SEED_PASSWORD", "senha-do-segredo-123")
    assert _rodar(monkeypatch, "--email", "op@teste.com", "--name", "Op") == 0
    assert "senha-do-segredo-123" not in capsys.readouterr().out
    assert verify_password("senha-do-segredo-123", _hash("op@teste.com"))


def test_sem_senha_nenhuma_gera_e_imprime(monkeypatch, capsys):
    monkeypatch.delenv("VISIONYARD_SEED_PASSWORD", raising=False)
    assert _rodar(monkeypatch, "--email", "op2@teste.com", "--name", "Op") == 0
    assert "senha:" in capsys.readouterr().out


def test_senha_curta_da_variavel_e_recusada(monkeypatch):
    monkeypatch.setenv("VISIONYARD_SEED_PASSWORD", "curta")
    assert _rodar(monkeypatch, "--email", "op3@teste.com", "--name", "Op") == 1
